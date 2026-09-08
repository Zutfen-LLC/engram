"""Versioned semantic served-context manifest contract.

``semantic-context-manifest-v1`` is intentionally independent from the frozen
startup ``context-manifest-v1`` contract.  It describes a packet that has
already been selected and rendered.  It never performs retrieval or reads a
memory row.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import Field, model_validator

from engram.context_manifest import (
    CANONICALIZATION,
    MEMORY_CONTEXT_VERSION,
    PACKET_MEDIA_TYPE,
    PACKET_RENDER_VERSION,
    CanonicalUuidV1,
    FiniteFloatV1,
    NonNegativeIntV1,
    OptionalCanonicalUuidV1,
    Sha256DigestV1,
    VisibilityV1,
    _StrictModel,
    canonical_json_bytes,
    reconstruct_working_set_v1,
    sha256_digest,
)

__all__ = [
    "SEMANTIC_MANIFEST_CONTRACT_VERSION",
    "SEMANTIC_MODE",
    "SemanticContextManifestV1",
    "SemanticManifestDecisionContextV1",
    "build_semantic_context_manifest_v1",
    "semantic_query_digest",
]

SEMANTIC_SCHEMA: Final[Literal["engram.semantic-context-manifest"]] = (
    "engram.semantic-context-manifest"
)
SEMANTIC_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
SEMANTIC_MODE: Literal["semantic"] = "semantic"
SEMANTIC_MANIFEST_CONTRACT_VERSION: Literal["semantic-context-manifest-v1"] = (
    "semantic-context-manifest-v1"
)
_QUERY_DOMAIN = b"engram.semantic-context-manifest-v1/query\x00"


def semantic_query_digest(query: str) -> str:
    """Hash exact UTF-8 query bytes with a versioned domain separator."""
    return sha256_digest(_QUERY_DOMAIN + query.encode("utf-8"))


class SemanticSubjectV1(_StrictModel):
    tenant_id: CanonicalUuidV1
    principal_id: CanonicalUuidV1
    workspace_id: OptionalCanonicalUuidV1
    memory_context_version: Literal["memory-context-v2"]
    memory_profile_id: OptionalCanonicalUuidV1
    memory_profile_revision_id: OptionalCanonicalUuidV1
    memory_profile_version: NonNegativeIntV1 | None

    @model_validator(mode="after")
    def profile_is_complete(self) -> SemanticSubjectV1:
        values = (
            self.memory_profile_id,
            self.memory_profile_revision_id,
            self.memory_profile_version,
        )
        if any(value is not None for value in values) and any(value is None for value in values):
            raise ValueError("memory profile identity must be all set or all null")
        return self


class SemanticRequestedV1(_StrictModel):
    workspace_supplied: bool
    byte_budget: NonNegativeIntV1 | None
    token_budget: NonNegativeIntV1 | None
    item_budget: NonNegativeIntV1 | None


class SemanticEffectiveV1(_StrictModel):
    workspace_id: OptionalCanonicalUuidV1
    byte_budget: NonNegativeIntV1 | None
    token_budget: NonNegativeIntV1 | None
    item_budget: NonNegativeIntV1 | None


class SemanticRequestV1(_StrictModel):
    requested: SemanticRequestedV1
    effective: SemanticEffectiveV1
    query_digest: Sha256DigestV1
    request_digest: Sha256DigestV1


class SemanticVersionsV1(_StrictModel):
    recall_profile: Literal["legacy", "governed", "exploratory"]
    recall_profile_contract_version: str
    scoring_version: str
    signals_version: str | None
    admission_policy: str | None
    relationship_relevance_version: str | None
    packing_version: str | None
    config_version: str
    manifest_contract_version: Literal["semantic-context-manifest-v1"]
    packet_render_version: Literal["working-set-v1"]


class SemanticResultV1(_StrictModel):
    item_count: NonNegativeIntV1
    served_content_byte_count: NonNegativeIntV1
    rendered_packet_byte_count: NonNegativeIntV1
    candidate_count: NonNegativeIntV1
    omitted_count: NonNegativeIntV1
    omitted_by_admission: dict[str, NonNegativeIntV1]
    expansion: dict[str, Any] | None
    packing: dict[str, Any] | None
    message: str | None


class SemanticPacketV1(_StrictModel):
    media_type: Literal["text/plain; charset=utf-8"]
    render_version: Literal["working-set-v1"]
    hash: Sha256DigestV1


class SemanticItemV1(_StrictModel):
    ordinal: NonNegativeIntV1
    item_id: CanonicalUuidV1
    kind: str
    served_content_hash: Sha256DigestV1
    review_status: str
    authority: int
    visibility: VisibilityV1
    workspace_id: OptionalCanonicalUuidV1
    pinned: bool
    score: FiniteFloatV1 | None
    trust_score: FiniteFloatV1 | None
    relevance_score: FiniteFloatV1 | None
    utility_score: FiniteFloatV1 | None
    reasons: list[str]
    warnings: list[str]
    warning_codes: list[str] | None
    conflict_type: str | None
    conflict_resolution_status: str | None
    admission: dict[str, Any] | None
    evidence: dict[str, Any] | None
    relationship: dict[str, Any] | None
    packing_reason: str | None

    @model_validator(mode="after")
    def profile_facts_are_coherent(self) -> SemanticItemV1:
        v2_values = (
            self.admission,
            self.evidence,
            self.relevance_score,
            self.utility_score,
        )
        if any(value is not None for value in v2_values) and any(
            value is None for value in v2_values
        ):
            raise ValueError("V2 item facts must be all set or all null")
        if self.admission is None:
            return self
        if not self.packing_reason:
            raise ValueError("V2 item requires an exact packing_reason")
        required_admission = {
            "profile",
            "decision",
            "policy_version",
            "surface",
            "surface_decision",
            "v2",
        }
        if not required_admission.issubset(self.admission):
            raise ValueError("V2 admission is missing required decision identity")
        v2 = self.admission["v2"]
        if not isinstance(v2, dict) or not isinstance(v2.get("fresh"), dict):
            raise ValueError("V2 admission requires a fresh decision identity")
        fresh = v2["fresh"]
        required_fresh = {
            "policy_version",
            "policy_artifact_digest",
            "decision_hash",
            "epistemic_state",
            "risk_state",
            "retention_state",
            "effective_assessment_refs",
        }
        if not required_fresh.issubset(fresh):
            raise ValueError("V2 fresh decision is missing required identity")
        assert self.evidence is not None
        evidence_keys = {
            "profile_key",
            "policy_version",
            "policy_artifact_digest",
            "decision_hash",
            "epistemic_state",
            "risk_state",
            "retention_state",
            "effective_assessment_refs",
        }
        if not evidence_keys.issubset(self.evidence):
            raise ValueError("V2 evidence is missing required identity")
        for key in evidence_keys - {"profile_key"}:
            if self.evidence[key] != fresh[key]:
                raise ValueError("V2 evidence must mirror the fresh decision identity")
        if self.evidence["profile_key"] != v2.get("profile_key"):
            raise ValueError("V2 evidence profile_key must mirror admission")
        if self.relationship is not None and not {"version", "origins"}.issubset(self.relationship):
            raise ValueError("relationship binding is missing version or origins")
        return self


class SemanticContextManifestV1(_StrictModel):
    schema_name: Literal["engram.semantic-context-manifest"] = Field(
        alias="schema", serialization_alias="schema"
    )
    schema_version: Literal["1.0"]
    canonicalization: Literal["rfc8785"]
    mode: Literal["semantic"]
    subject: SemanticSubjectV1
    request: SemanticRequestV1
    versions: SemanticVersionsV1
    result: SemanticResultV1
    packet: SemanticPacketV1
    items: list[SemanticItemV1]

    @model_validator(mode="after")
    def coherent(self) -> SemanticContextManifestV1:
        if self.result.item_count != len(self.items):
            raise ValueError("result.item_count does not match items")
        for ordinal, item in enumerate(self.items):
            if item.ordinal != ordinal:
                raise ValueError("item ordinal does not match array order")
        return self


class SemanticManifestDecisionContextV1(_StrictModel):
    """Executed semantic decision facts.  This is input, not a wire artifact."""

    tenant_id: CanonicalUuidV1
    principal_id: CanonicalUuidV1
    workspace_id: OptionalCanonicalUuidV1
    memory_context_version: Literal["memory-context-v2"] = MEMORY_CONTEXT_VERSION
    memory_profile_id: OptionalCanonicalUuidV1
    memory_profile_revision_id: OptionalCanonicalUuidV1
    memory_profile_version: NonNegativeIntV1 | None
    workspace_supplied: bool
    requested_byte_budget: NonNegativeIntV1 | None
    requested_token_budget: NonNegativeIntV1 | None
    requested_item_budget: NonNegativeIntV1 | None
    effective_byte_budget: NonNegativeIntV1 | None
    effective_token_budget: NonNegativeIntV1 | None
    effective_item_budget: NonNegativeIntV1 | None
    recall_profile: Literal["legacy", "governed", "exploratory"]
    recall_profile_contract_version: str
    scoring_version: str
    signals_version: str | None
    admission_policy: str | None
    relationship_relevance_version: str | None
    packing_version: str | None
    config_version: str


def _item(item: dict[str, Any], ordinal: int) -> SemanticItemV1:
    required = ("id", "kind", "content", "review_status", "authority", "visibility", "pinned")
    missing = [name for name in required if name not in item]
    if missing:
        raise ValueError(f"finalized semantic item is missing fields: {', '.join(missing)}")
    return SemanticItemV1(
        ordinal=ordinal,
        item_id=item["id"],
        kind=item["kind"],
        served_content_hash=sha256_digest(item["content"].encode("utf-8")),
        review_status=item["review_status"],
        authority=item["authority"],
        visibility=item["visibility"],
        workspace_id=item.get("workspace_id"),
        pinned=item["pinned"],
        score=item.get("score"),
        trust_score=item.get("trust_score"),
        relevance_score=item.get("relevance_score"),
        utility_score=item.get("utility_score"),
        reasons=list(item.get("reasons", [])),
        warnings=list(item.get("warnings", [])),
        warning_codes=(
            list(item["warning_codes"]) if item.get("warning_codes") is not None else None
        ),
        conflict_type=item.get("conflict_type"),
        conflict_resolution_status=item.get("conflict_resolution_status"),
        admission=item.get("admission"),
        evidence=item.get("evidence"),
        relationship=item.get("relationship"),
        packing_reason=item.get("packing_reason"),
    )


def build_semantic_context_manifest_v1(
    *,
    items: list[dict[str, Any]],
    working_set: str,
    item_count: int,
    byte_count: int,
    candidate_count: int,
    omitted_count: int,
    omitted_by_admission: dict[str, int],
    expansion: dict[str, Any] | None,
    packing: dict[str, Any] | None,
    message: str | None,
    query: str,
    context: SemanticManifestDecisionContextV1,
) -> SemanticContextManifestV1:
    """Build a semantic manifest only from a finalized packet snapshot."""
    if item_count != len(items):
        raise ValueError("finalized item_count does not match items")
    actual_bytes = sum(len(item["content"].encode("utf-8")) for item in items)
    if byte_count != actual_bytes:
        raise ValueError("finalized byte_count does not match item content")
    if reconstruct_working_set_v1(items) != working_set:
        raise ValueError("finalized working_set does not match rendered items")
    for budget, actual, name in (
        (context.effective_byte_budget, byte_count, "byte"),
        (context.effective_item_budget, item_count, "item"),
    ):
        if budget is not None and actual > budget:
            raise ValueError(f"finalized packet exceeds effective {name} budget")
    rendered_token_count = (len(working_set.encode("utf-8")) + 3) // 4
    if (
        context.effective_token_budget is not None
        and rendered_token_count > context.effective_token_budget
    ):
        raise ValueError("finalized packet exceeds effective token budget")
    if context.recall_profile == "legacy" and any(
        item.get("admission") is not None or item.get("evidence") is not None for item in items
    ):
        raise ValueError("legacy semantic packet must not fabricate V2 facts")
    if context.recall_profile != "legacy" and (
        packing is None or packing.get("selected_count") != item_count
    ):
        raise ValueError("candidate packet packing summary does not match selected items")
    built_items = [_item(item, ordinal) for ordinal, item in enumerate(items)]
    query_digest = semantic_query_digest(query)
    requested = SemanticRequestedV1(
        workspace_supplied=context.workspace_supplied,
        byte_budget=context.requested_byte_budget,
        token_budget=context.requested_token_budget,
        item_budget=context.requested_item_budget,
    )
    effective = SemanticEffectiveV1(
        workspace_id=context.workspace_id,
        byte_budget=context.effective_byte_budget,
        token_budget=context.effective_token_budget,
        item_budget=context.effective_item_budget,
    )
    request_input = {
        "requested": requested.model_dump(mode="json", exclude_none=False),
        "effective": effective.model_dump(mode="json", exclude_none=False),
        "query_digest": query_digest,
    }
    manifest = SemanticContextManifestV1(
        schema=SEMANTIC_SCHEMA,
        schema_version=SEMANTIC_SCHEMA_VERSION,
        canonicalization=CANONICALIZATION,
        mode=SEMANTIC_MODE,
        subject=SemanticSubjectV1(
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            workspace_id=context.workspace_id,
            memory_context_version=context.memory_context_version,
            memory_profile_id=context.memory_profile_id,
            memory_profile_revision_id=context.memory_profile_revision_id,
            memory_profile_version=context.memory_profile_version,
        ),
        request=SemanticRequestV1(
            requested=requested,
            effective=effective,
            query_digest=query_digest,
            request_digest=sha256_digest(canonical_json_bytes(request_input)),
        ),
        versions=SemanticVersionsV1(
            recall_profile=context.recall_profile,
            recall_profile_contract_version=context.recall_profile_contract_version,
            scoring_version=context.scoring_version,
            signals_version=context.signals_version,
            admission_policy=context.admission_policy,
            relationship_relevance_version=context.relationship_relevance_version,
            packing_version=context.packing_version,
            config_version=context.config_version,
            manifest_contract_version=SEMANTIC_MANIFEST_CONTRACT_VERSION,
            packet_render_version=PACKET_RENDER_VERSION,
        ),
        result=SemanticResultV1(
            item_count=item_count,
            served_content_byte_count=byte_count,
            rendered_packet_byte_count=len(working_set.encode("utf-8")),
            candidate_count=candidate_count,
            omitted_count=omitted_count,
            omitted_by_admission=omitted_by_admission,
            expansion=expansion,
            packing=packing,
            message=message,
        ),
        packet=SemanticPacketV1(
            media_type=PACKET_MEDIA_TYPE,
            render_version=PACKET_RENDER_VERSION,
            hash=sha256_digest(working_set.encode("utf-8")),
        ),
        items=built_items,
    )
    # Validate arbitrary nested decision data with the same RFC 8785 encoder
    # used by the manifest hash. This rejects NaN, infinity, and non-JSON
    # values before the builder returns a seemingly valid artifact.
    canonical_json_bytes(manifest.model_dump(mode="json", by_alias=True, exclude_none=False))
    return manifest
