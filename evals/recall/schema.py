"""Validated private inputs for read-only recall evaluation."""

from __future__ import annotations

from typing import Annotated, Any, Literal
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
    """Reviewed item state bound to one frozen case and snapshot.

    The values describe the reviewed item, not a result for a recall profile.
    Candidate-specific effects are derived from packet membership by the
    metrics module.  Empty labels need no binding because they are not
    evidence.
    """

    case_id: Token | None = None
    snapshot_digest: Digest | None = None
    label_set_digest: Digest | None = None
    contamination: dict[str, Literal["contaminated", "acceptable", "unknown"]] = Field(
        default_factory=dict
    )
    usefulness: dict[str, Literal["useful", "unknown"]] = Field(default_factory=dict)

    def canonical_identity(self) -> dict[str, Any]:
        """Return the label content that the reviewer seals with a digest."""
        return {
            "contamination": dict(sorted(self.contamination.items())),
            "usefulness": dict(sorted(self.usefulness.items())),
        }

    def is_empty(self) -> bool:
        return not self.contamination and not self.usefulness

    def validate_binding(self, *, case_id: str, snapshot_digest: str) -> None:
        """Reject labels that cannot be tied to this frozen replay input."""
        if self.is_empty():
            if any((self.case_id, self.snapshot_digest, self.label_set_digest)):
                raise ValueError("empty_labels_must_not_have_binding")
            return
        if self.case_id != case_id:
            raise ValueError("label_case_id_mismatch")
        if self.snapshot_digest != snapshot_digest:
            raise ValueError("label_snapshot_digest_mismatch")
        if self.label_set_digest != digest(self.canonical_identity()):
            raise ValueError("label_set_digest_mismatch")


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
    """Private replay manifest.

    ``input_digest`` hashes the complete material input.  The public identity
    intentionally does not: public artifacts expose that digest, rather than
    tenant, principal, workspace, or query identity.
    """

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

    @model_validator(mode="after")
    def unique_case_ids(self) -> RecallEvaluationManifest:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_recall_case_id")
        for case in self.cases:
            case.labels.validate_binding(
                case_id=case.case_id,
                snapshot_digest=self.snapshot_digest,
            )
        return self

    def private_input_identity(self) -> dict[str, Any]:
        """Return every input that can change a replay result.

        This value is private because it includes the raw request boundary and
        query text.  It is only ever used as digest input or in the protected
        private artifact.
        """
        return self.model_dump(mode="json")

    def public_identity(self) -> dict[str, object]:
        """Return reproducibility metadata that cannot contain query text."""
        return {
            "schema_version": self.schema_version,
            "baseline_sha": self.baseline_sha,
            "repository_sha": self.repository_sha,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_at": self.snapshot_at.isoformat(),
            "evaluation_at": self.evaluation_at.isoformat(),
            "embedding_profile_key": self.embedding_profile_key,
            "case_count": len(self.cases),
        }

    @property
    def input_digest(self) -> str:
        return digest(self.private_input_identity())
