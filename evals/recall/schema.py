"""Validated private inputs for read-only recall evaluation."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from engram.memory_context import ResolvedMemoryContext
from engram.semantic_context_manifest import semantic_query_digest
from evals.admission.schema import Digest, Token, digest

QueryDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryContextSnapshot(_Record):
    """Frozen request identity and visibility boundary for every replay case."""

    version: Literal["memory-context-v2"]
    tenant_id: UUID
    principal_id: UUID
    api_key_id: UUID | None = None
    memory_profile_id: UUID | None = None
    memory_profile_revision_id: UUID | None = None
    memory_profile_slug: str | None = None
    memory_profile_version: int | None = None
    include_private: bool = True
    include_tenant: bool = True
    include_public: bool = True
    readable_workspace_ids: tuple[UUID, ...] | None = None
    allow_tenant_write: bool = True
    allow_public_write: bool = True
    default_write_visibility: Literal["private", "workspace", "tenant", "public"] = "private"
    default_write_workspace_id: UUID | None = None
    writable_workspace_ids: tuple[UUID, ...] | None = None
    admin_workspace_bypass: bool = False

    def resolve(self) -> ResolvedMemoryContext:
        """Build the production read boundary without an HTTP request."""
        return ResolvedMemoryContext(
            **self.model_dump(
                mode="python",
                exclude={"readable_workspace_ids", "writable_workspace_ids"},
            ),
            readable_workspace_ids=(
                frozenset(self.readable_workspace_ids)
                if self.readable_workspace_ids is not None
                else None
            ),
            writable_workspace_ids=(
                frozenset(self.writable_workspace_ids)
                if self.writable_workspace_ids is not None
                else None
            ),
        )


class RecallCaseLabels(_Record):
    """Optional reviewed labels. Unlabeled cases stay unknown."""

    contamination: dict[str, Literal["avoided", "introduced", "unknown"]] = Field(
        default_factory=dict
    )
    usefulness: dict[str, Literal["useful", "unknown"]] = Field(default_factory=dict)


class RecallQueryCase(_Record):
    case_id: Token
    query: str = Field(min_length=1)
    query_digest: QueryDigest
    workspace: str | None = None
    byte_budget: int | None = Field(default=None, ge=0)
    token_budget: int | None = Field(default=None, ge=0)
    item_budget: int | None = Field(default=None, ge=0)
    strata: dict[Token, Token] = Field(default_factory=dict)
    labels: RecallCaseLabels = Field(default_factory=RecallCaseLabels)

    @model_validator(mode="after")
    def query_identity_matches(self) -> RecallQueryCase:
        if self.query_digest != semantic_query_digest(self.query):
            raise ValueError("query_digest_mismatch")
        return self


class RecallEvaluationManifest(_Record):
    """Private replay manifest. Its digest omits query text and raw IDs."""

    schema_version: Literal["engram-recall-evaluation-input-v1"]
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    repository_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_digest: Digest
    snapshot_at: AwareDatetime
    evaluation_at: AwareDatetime
    tenant_config_version: Token
    embedding_profile_key: Token
    memory_context: MemoryContextSnapshot
    cases: tuple[RecallQueryCase, ...] = Field(min_length=1)
    terminal_recommendation: Literal[
        "READY_FOR_162_RECALL_CERTIFICATION",
        "RECALL_CORRECTION_REQUIRED",
        "INCONCLUSIVE",
    ] = "INCONCLUSIVE"

    @model_validator(mode="after")
    def unique_case_ids(self) -> RecallEvaluationManifest:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_recall_case_id")
        return self

    def public_identity(self) -> dict[str, object]:
        """Return reproducibility metadata that cannot contain query text."""
        return {
            "schema_version": self.schema_version,
            "baseline_sha": self.baseline_sha,
            "repository_sha": self.repository_sha,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_at": self.snapshot_at.isoformat(),
            "evaluation_at": self.evaluation_at.isoformat(),
            "tenant_config_version": self.tenant_config_version,
            "embedding_profile_key": self.embedding_profile_key,
            "case_count": len(self.cases),
            "case_digests": [case.query_digest for case in self.cases],
        }

    @property
    def input_digest(self) -> str:
        return digest(self.public_identity())
