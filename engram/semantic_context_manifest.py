"""Versioned semantic served-context manifest contract.

``semantic-context-manifest-v1`` is intentionally independent from the frozen
startup ``context-manifest-v1`` contract.  It describes a packet that has
already been selected and rendered.  It never performs retrieval or reads a
memory row.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import Field, model_validator

from engram.admission_assessment import AdmissionNextAction, AdmissionOutcome
from engram.admission_assessment_schema import (
    AdmissionRetentionState,
    AdmissionRiskState,
    AdmissionTier,
)
from engram.assessment_schema import AssertionMode, Origin
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
from engram.recall_packing import RECALL_PACKING_VERSION, PackingReason
from engram.recall_signals import (
    RECALL_ADMISSION_POLICY_VERSION,
    WarningCode,
)
from engram.review_policy import ReviewStatus
from engram.semantic_budget import semantic_item_byte_count, semantic_item_token_cost

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

# These receipt aliases mirror database CHECK constraints that predate a
# shared Python type. Tests bind the sets mechanically to migrations/001_init.sql.
SemanticConflictTypeV1 = Literal["contradiction", "stale", "duplicate", "scope_overlap"]
SemanticConflictResolutionStatusV1 = Literal["unresolved", "accepted", "rejected", "merged"]

# The V2 evaluator supports exactly one policy profile. Keep this receipt wire
# alias local so importing this pure contract does not depend on admission_shadow.
# A focused anti-drift test binds it to recall_signals.V2_ADMISSION_PROFILE_KEY.
SemanticV2ProfileKeyV1 = Literal["risk_aware_shadow_v1"]


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
    admission_policy: Literal["recall-admission-v2"] | None
    relationship_relevance_version: Literal["relationship-relevance-v1"] | None
    packing_version: Literal["recall-packing-v1"] | None
    config_version: str
    manifest_contract_version: Literal["semantic-context-manifest-v1"]
    packet_render_version: Literal["working-set-v1"]


class SemanticExpansionV1(_StrictModel):
    """The bounded relationship-expansion summary produced by issue #190."""

    version: Literal["relationship-relevance-v1"]
    seed_count: NonNegativeIntV1
    discovered_neighbors: NonNegativeIntV1
    graph_neighbors: NonNegativeIntV1
    tunnel_neighbors: NonNegativeIntV1
    admitted_expanded: NonNegativeIntV1
    withheld_expanded: NonNegativeIntV1


class SemanticPackingOmittedV1(_StrictModel):
    """Sparse omission counts from ``recall-packing-v1``."""

    redundant_known_root: NonNegativeIntV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    conflict_counterpart_budget: NonNegativeIntV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    budget: NonNegativeIntV1 | None = Field(default=None, exclude_if=lambda value: value is None)


class SemanticPackingV1(_StrictModel):
    """The bounded packet summary produced by issue #192."""

    version: Literal["recall-packing-v1"]
    selected_count: NonNegativeIntV1
    conflict_pairs_preserved: NonNegativeIntV1
    omitted: SemanticPackingOmittedV1


class SemanticResultV1(_StrictModel):
    item_count: NonNegativeIntV1
    served_content_byte_count: NonNegativeIntV1
    rendered_packet_byte_count: NonNegativeIntV1
    candidate_count: NonNegativeIntV1
    omitted_count: NonNegativeIntV1
    omitted_by_admission: dict[str, NonNegativeIntV1]
    expansion: SemanticExpansionV1 | None
    packing: SemanticPackingV1 | None
    message: str | None


class SemanticPacketV1(_StrictModel):
    media_type: Literal["text/plain; charset=utf-8"]
    render_version: Literal["working-set-v1"]
    hash: Sha256DigestV1


class SemanticAssessmentRefV1(_StrictModel):
    """One effective #157 assessment reference used by V2."""

    assessment_id: CanonicalUuidV1
    contract_hash: Sha256DigestV1
    canonical_hash: Sha256DigestV1
    purpose: Literal["combined"]
    assertion_mode: AssertionMode
    origin: Origin


class SemanticV2PersistedV1(_StrictModel):
    """The persisted V2 decision identity used by candidate admission."""

    assessment_id: CanonicalUuidV1
    schema_version: Literal["engram.admission-assessment.v2"]
    policy_contract_version: str
    policy_artifact_digest: Sha256DigestV1
    decision_hash: Sha256DigestV1


class SemanticV2FreshV1(_StrictModel):
    """The exact safe fresh V2 decision identity from issue #186."""

    schema_version: Literal["engram.admission-assessment.v2"]
    policy_version: str
    policy_artifact_digest: Sha256DigestV1
    decision_hash: Sha256DigestV1
    surface_decision: Literal["allow"]
    highest_admission_tier: AdmissionTier | None
    risk_state: AdmissionRiskState | None
    epistemic_state: Literal["supported", "contested", "insufficient_evidence", "unknown"]
    retention_state: AdmissionRetentionState | None
    effective_assessment_refs: list[SemanticAssessmentRefV1]
    observation_window_hours: NonNegativeIntV1 | None
    eligible_at: str | None
    next_evaluation_at: str | None
    blocker_codes: list[str]
    reason_codes: list[str]
    next_actions: list[AdmissionNextAction]


class SemanticV2BindingV1(_StrictModel):
    """The V2 resolution identity consumed by the admission decision."""

    profile_key: SemanticV2ProfileKeyV1
    resolution_status: Literal["current"]
    surface: Literal["semantic_governed", "semantic_exploratory"]
    surface_decision: Literal["allow"]
    persisted: SemanticV2PersistedV1
    fresh: SemanticV2FreshV1

    @model_validator(mode="after")
    def persisted_and_fresh_agree(self) -> SemanticV2BindingV1:
        if self.surface_decision != self.fresh.surface_decision:
            raise ValueError("V2 surface decision does not match fresh decision")
        for persisted_name, fresh_name in (
            ("schema_version", "schema_version"),
            ("policy_contract_version", "policy_version"),
            ("policy_artifact_digest", "policy_artifact_digest"),
            ("decision_hash", "decision_hash"),
        ):
            if getattr(self.persisted, persisted_name) != getattr(self.fresh, fresh_name):
                raise ValueError("current V2 persisted and fresh identities must match")
        return self


class SemanticAdmissionV1(_StrictModel):
    """The safe candidate admission receipt from issues #186 and #188."""

    profile: Literal["governed", "exploratory"]
    decision: Literal["admit"]
    policy_version: Literal["recall-admission-v2"]
    reason_codes: list[str]
    assessment_id: CanonicalUuidV1 | None
    assessment_status: Literal["current", "stale", "legacy_import"] | None
    assessment_outcome: AdmissionOutcome | None
    surface: Literal["semantic_governed", "semantic_exploratory"]
    surface_decision: Literal["allow"]
    v2: SemanticV2BindingV1

    @model_validator(mode="after")
    def identity_is_coherent(self) -> SemanticAdmissionV1:
        expected_surface = (
            "semantic_governed" if self.profile == "governed" else "semantic_exploratory"
        )
        if self.surface != expected_surface or self.v2.surface != expected_surface:
            raise ValueError("admission profile and V2 surface do not match")
        if self.surface_decision != self.v2.surface_decision:
            raise ValueError("admission and V2 surface decisions do not match")
        if self.assessment_id is not None and self.assessment_id != self.v2.persisted.assessment_id:
            raise ValueError("admission assessment identity does not match V2")
        return self


class SemanticEvidenceV1(_StrictModel):
    """The canonical issue #188 evidence projection."""

    source: Literal["v2_fresh_evaluation"]
    profile_key: SemanticV2ProfileKeyV1
    policy_version: str
    policy_artifact_digest: Sha256DigestV1
    decision_hash: Sha256DigestV1
    v2_resolution_status: Literal["current"]
    epistemic_state: Literal["supported", "contested", "insufficient_evidence", "unknown"]
    risk_state: AdmissionRiskState | None
    retention_state: AdmissionRetentionState | None
    effective_assessment_refs: list[SemanticAssessmentRefV1]


class SemanticRelationshipComponentsV1(_StrictModel):
    semantic: FiniteFloatV1
    graph: FiniteFloatV1
    tunnel: FiniteFloatV1


class SemanticRelationshipV1(_StrictModel):
    """The finalized ``relationship-relevance-v1`` item block."""

    version: Literal["relationship-relevance-v1"]
    origins: list[Literal["semantic", "graph", "tunnel"]]
    direct: bool
    direct_semantic_score: FiniteFloatV1 | None
    source_seed_score: FiniteFloatV1
    graph_contribution: FiniteFloatV1
    graph_edge_types: list[str]
    tunnel_labels: list[str]
    relevance_score: FiniteFloatV1
    components: SemanticRelationshipComponentsV1


class SemanticItemV1(_StrictModel):
    ordinal: NonNegativeIntV1
    item_id: CanonicalUuidV1
    kind: str
    served_content_hash: Sha256DigestV1
    review_status: ReviewStatus
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
    warning_codes: list[WarningCode] | None
    conflict_type: SemanticConflictTypeV1 | None
    conflict_resolution_status: SemanticConflictResolutionStatusV1 | None
    admission: SemanticAdmissionV1 | None
    evidence: SemanticEvidenceV1 | None
    relationship: SemanticRelationshipV1 | None
    packing_reason: PackingReason | None

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
        v2 = self.admission.v2
        fresh = v2.fresh
        assert self.evidence is not None
        for name in (
            "policy_version",
            "policy_artifact_digest",
            "decision_hash",
            "epistemic_state",
            "risk_state",
            "retention_state",
            "effective_assessment_refs",
        ):
            if getattr(self.evidence, name) != getattr(fresh, name):
                raise ValueError("V2 evidence must mirror the fresh decision identity")
        if self.evidence.profile_key != v2.profile_key:
            raise ValueError("V2 evidence profile_key must mirror admission")
        if self.evidence.v2_resolution_status != v2.resolution_status:
            raise ValueError("V2 evidence resolution status must mirror admission")
        if (
            self.relationship is not None
            and self.relationship.relevance_score != self.relevance_score
        ):
            raise ValueError("relationship relevance must match the served relevance score")
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
        if self.result.packing is not None and self.result.packing.selected_count != len(
            self.items
        ):
            raise ValueError("packing selected_count does not match items")
        if (self.result.expansion is None) != (
            self.versions.relationship_relevance_version is None
        ):
            raise ValueError("expansion and relationship version must be present together")
        if self.result.expansion is not None and (
            self.versions.relationship_relevance_version != self.result.expansion.version
        ):
            raise ValueError("expansion relationship version does not match versions")
        if (self.result.packing is None) != (self.versions.packing_version is None):
            raise ValueError("packing and packing version must be present together")
        if self.result.packing is not None and (
            self.versions.packing_version != self.result.packing.version
        ):
            raise ValueError("packing version does not match versions")
        if self.versions.recall_profile == "legacy":
            if any(
                value is not None
                for value in (
                    self.versions.admission_policy,
                    self.versions.relationship_relevance_version,
                    self.versions.packing_version,
                )
            ):
                raise ValueError("legacy profile must not declare candidate protocol versions")
        elif (
            self.versions.admission_policy != RECALL_ADMISSION_POLICY_VERSION
            or self.versions.packing_version != RECALL_PACKING_VERSION
        ):
            raise ValueError("candidate profile requires exact admission and packing versions")
        for ordinal, item in enumerate(self.items):
            if item.ordinal != ordinal:
                raise ValueError("item ordinal does not match array order")
            if item.admission is not None and (
                item.admission.profile != self.versions.recall_profile
            ):
                raise ValueError("item admission profile does not match manifest profile")
            if item.relationship is not None and (
                item.relationship.version != self.versions.relationship_relevance_version
            ):
                raise ValueError("item relationship contract does not match versions")
            candidate_values = (
                item.admission,
                item.evidence,
                item.relevance_score,
                item.utility_score,
                item.packing_reason,
            )
            if self.versions.recall_profile == "legacy" and any(
                value is not None for value in (*candidate_values, item.relationship)
            ):
                raise ValueError("legacy item must not carry candidate profile facts")
            if self.versions.recall_profile != "legacy" and any(
                value is None for value in candidate_values
            ):
                raise ValueError("candidate item requires exact V2 and packing facts")
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
    admission_policy: Literal["recall-admission-v2"] | None
    relationship_relevance_version: Literal["relationship-relevance-v1"] | None
    packing_version: Literal["recall-packing-v1"] | None
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
    actual_bytes = sum(semantic_item_byte_count(item["content"]) for item in items)
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
    selected_token_count = sum(semantic_item_token_cost(item["content"]) for item in items)
    if (
        context.effective_token_budget is not None
        and selected_token_count > context.effective_token_budget
    ):
        raise ValueError("finalized packet exceeds effective token budget")
    if context.recall_profile == "legacy":
        if any(
            value is not None
            for value in (
                context.admission_policy,
                context.relationship_relevance_version,
                context.packing_version,
            )
        ):
            raise ValueError("legacy semantic packet must not declare candidate versions")
        candidate_fields = (
            "admission",
            "evidence",
            "relationship",
            "packing_reason",
            "relevance_score",
            "utility_score",
        )
        if any(item.get(field) is not None for item in items for field in candidate_fields):
            raise ValueError("legacy semantic packet must not fabricate candidate facts")
    elif (
        context.admission_policy != RECALL_ADMISSION_POLICY_VERSION
        or context.packing_version != RECALL_PACKING_VERSION
    ):
        raise ValueError("candidate packet requires exact admission and packing versions")
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
            expansion=(
                SemanticExpansionV1.model_validate(expansion) if expansion is not None else None
            ),
            packing=(SemanticPackingV1.model_validate(packing) if packing is not None else None),
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
