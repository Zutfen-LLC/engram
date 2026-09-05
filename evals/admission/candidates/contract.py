"""Versioned, deterministic candidate-policy result contract (#162C).

Candidate policies are non-authoritative shadow experiments. A candidate result
is immutable, content-addressable, and evaluates the five independent decision
surfaces separately. Candidate decision code receives only
:class:`evals.admission.policy.PolicyInput` — the same captured decision-time
state production would see — and can never consume frozen human labels.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, Field

from evals.admission.policy import ConfigSnapshot, PolicyInput
from evals.admission.schema import Record, Token

StorageDisposition = Literal["retain", "defer", "reject", "unknown"]
TriState = Literal["yes", "no", "unknown"]
NextAction = Literal["automatic_admission", "review", "wait", "reject", "none", "unknown"]

RESULT_SCHEMA_VERSION: Literal["engram-candidate-policy-result-v1"] = (
    "engram-candidate-policy-result-v1"
)

#: Closed vocabulary of decision-time signals a profile may require. Values are
#: field names of PolicyInput or derived canonical evaluator states. Anything a
#: profile needs that is not in this vocabulary must be declared as unavailable.
SIGNAL_VOCABULARY: frozenset[str] = frozenset(
    {
        "source_type",
        "kind",
        "kind_enabled",
        "kind_auto_promote",
        "review_status",
        "live",
        "superseded",
        "created_at",
        "memory_confidence",
        "source_confidence_prior",
        "retention_confidence",
        "retention_disposition",
        "retention_evidence_at",
        "conflict_resolution_status",
        "external_dispute",
        "external_noise",
        "receipt",
        "receipt.taxonomy_confidence",
        "receipt.binding_matches",
        "receipt.classification_version",
        "evidence_state",
        "evidence_eligible_at",
        "job_state",
        "recalled",
    }
)


class CandidateParameters(Record):
    """Declared, frozen numeric parameters. No value may be fitted on labels."""

    evidence_threshold: float = Field(ge=0, le=1)
    taxonomy_minimum: float = Field(ge=0, le=1)
    legacy_confidence_threshold: float = Field(ge=0, le=1)
    deferral_window_hours: int = Field(ge=0)
    deferral_window_provenance: Token


class CandidateDeclaration(Record):
    """The discipline contract every profile must publish before evaluation."""

    policy_version: Token
    hypothesis: str
    required_input_signals: tuple[Token, ...]
    protected_kind_behavior: str
    unknown_behavior: str
    automatic_admission_conditions: tuple[str, ...]
    review_routing_conditions: tuple[str, ...]
    storage_semantics: str
    governed_semantics: str
    startup_semantics: str
    parameters: CandidateParameters
    parameter_provenance: str
    oracle: Literal["no", "yes"] = "no"

    def model_post_init(self, __context: Any) -> None:
        if self.oracle != "no":
            raise ValueError("deployable_declaration_cannot_be_oracle")
        self._validate_signals()

    def _validate_signals(self) -> None:
        unknown_signals = set(self.required_input_signals) - SIGNAL_VOCABULARY
        if unknown_signals:
            raise ValueError(f"undeclared_required_signal:{sorted(unknown_signals)[0]}")


class CandidateResult(Record):
    """One deterministic candidate decision. Non-authoritative by contract."""

    result_schema_version: Literal["engram-candidate-policy-result-v1"] = (
        RESULT_SCHEMA_VERSION
    )
    candidate_policy_version: Token
    storage_disposition: StorageDisposition
    automatic_admission: TriState
    governed_semantic_eligibility: TriState
    startup_eligibility: TriState
    human_review_required: TriState
    reason_codes: tuple[Token, ...] = ()
    required_signals: tuple[Token, ...] = ()
    unavailable_signals: tuple[Token, ...] = ()
    selected_lane: Token | None = None
    # Production evidence states include "malformed/stale"; Token's pattern
    # excludes "/". Free-form, content-free state label.
    evidence_state: str | None = None
    risk_estimate: Token | None = None
    next_evaluation_at: AwareDatetime | None = None
    next_action: NextAction = "unknown"

    def model_post_init(self, __context: Any) -> None:
        # "yes" admission and "yes" review are mutually exclusive: a review
        # requirement must never be counted as silent automatic admission.
        if self.automatic_admission == "yes" and self.human_review_required == "yes":
            raise ValueError("automatic_admission_conflicts_with_review")


class CandidatePolicy:
    """Base class for deployable-shape candidate policies (non-oracle)."""

    declaration: CandidateDeclaration

    def evaluate(
        self, item: PolicyInput, config: ConfigSnapshot | None, now: AwareDatetime
    ) -> CandidateResult:
        raise NotImplementedError

    def version(self) -> Token:
        return self.declaration.policy_version


class OracleDeclaration(CandidateDeclaration):
    """Declaration for analyses that consume human truth as an input.

    Oracle analyses quantify the value of signals Engram does not possess.
    They are typed separately from :class:`CandidatePolicy` so the type
    system, not reviewer attention, excludes them from shortlists.
    """

    oracle: Literal["yes"] = "yes"
    oracle_inputs: tuple[Token, ...] = ()
    oracle_question: str = ""

    def model_post_init(self, __context: Any) -> None:
        if self.oracle != "yes":
            raise ValueError("oracle_declaration_required")
        self._validate_signals()


class OraclePolicy:
    """Base class for oracle analyses. Never a deployable candidate."""

    declaration: OracleDeclaration

    def evaluate(
        self,
        item: PolicyInput,
        config: ConfigSnapshot | None,
        now: AwareDatetime,
        *,
        oracle_truth: dict[str, Any],
    ) -> CandidateResult:
        raise NotImplementedError

    def version(self) -> Token:
        return self.declaration.policy_version
