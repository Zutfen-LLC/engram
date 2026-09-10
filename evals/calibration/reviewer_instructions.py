"""Canonical frozen reviewer instruction bundle + deterministic response parser.

FIX-R4-6: the emitted reviewer instructions previously named only the guide
version (``engram-calibration-guide-157-v1``). An external Claude/GPT/GLM
session has no guaranteed knowledge of what that repository-local version
name means, so the five critical fields had names but no decision semantics
in the actual prompt.

FIX-R5-5 (provenance correction — truthful sourcing): this module freezes
the ACTUAL semantics into a canonical instruction bundle. The material
splits into two honestly-labeled layers:

- INHERITED from the frozen sources (``evals/labeling/calibration-157-v1.md``
  + ``evals/labeling/admission-v1.md``): the expected-kind closed
  vocabulary, the judge-independently-of-governed-kind principle, the
  ``unknown``-for-custom/unresolved-kinds rule, retention semantics
  (durable usefulness, never truth), epistemic definitions (decision-time
  evidence only), consequence definitions (of erroneous silent admission),
  abstention principles, and the honor rules;
- NEW #206 REVIEWER OPERATIONALIZATION (frozen here BEFORE execution under
  ``REVIEWER_INSTRUCTIONS_VERSION`` =
  ``engram-calibration-reviewer-instructions-206-v1``): the detailed
  per-kind definitions (fact / observation / decision / procedure /
  summary / doctrine / invariant / preference / diary_entry) and the exact
  decision-rule wording. These are pre-execution clarifications introduced
  by #206 — NOT text inherited from the old guide, which never contained a
  per-kind prose taxonomy. The old guide remains the underlying
  calibration guide (``label_guide_version`` is unchanged).

The complete bundle is:

- embedded in FULL in every emitted model request;
- bound by ``ReviewerIdentity.prompt_digest`` (via
  ``labeling_instructions_digest`` in ``evals.calibration.ingestion``), so a
  reviewer identity whose prompt digest does not match the exact bytes the
  lane emits cannot initialize or load;
- changing digest whenever any semantic rule changes.

FIX-R4-2: this module also owns the ONE deterministic parser from preserved
model-response bytes to a ``ModelJudgment``. The parsed judgment stored on a
``ModelReviewRecord`` is always derived here from the exact bytes — never
supplied as an independent sibling assertion. The parser version is frozen
and recorded on every parsed record.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from evals.calibration.consensus import ModelJudgment

# Frozen parser identity. Any change to parse_model_response's behavior MUST
# change this string (and therefore every new record's parser_version).
RESPONSE_PARSER_VERSION: Literal["model-response-parser-206-v1"] = "model-response-parser-206-v1"
# The response envelope the model itself must produce, preserved byte-exact.
RESPONSE_SCHEMA_NAME: Literal["engram-calibration-model-response-206-v1"] = (
    "engram-calibration-model-response-206-v1"
)

# FIX-R5-5: explicit identity for the #206 reviewer operationalization —
# the pre-execution clarification layer containing the detailed per-kind
# definitions and exact decision-rule wording. Frozen BEFORE any reviewer
# receives a packet (no real model review has executed, so no resampling or
# Round-2 invalidation is required). Distinct from the underlying guide
# version ``engram-calibration-guide-157-v1``, which is preserved unchanged.
REVIEWER_INSTRUCTIONS_VERSION: Literal["engram-calibration-reviewer-instructions-206-v1"] = (
    "engram-calibration-reviewer-instructions-206-v1"
)

# --- Frozen semantic bundle (FIX-R4-6 / FIX-R5-5) --------------------------
# Layered provenance: vocabulary + principles inherited from the frozen 157
# guide + admission handbook; detailed per-kind definitions and exact rule
# wording are the NEW #206 reviewer operationalization (see
# REVIEWER_INSTRUCTIONS_VERSION). Do not edit after merge without a new
# protocol version: every emitted request embeds this bundle and every
# reviewer identity binds its digest.
CANONICAL_SEMANTIC_BUNDLE: dict[str, Any] = {
    "expected_kind": {
        "allowed_vocabulary": [
            "preference",
            "fact",
            "observation",
            "decision",
            "procedure",
            "summary",
            "doctrine",
            "invariant",
            "diary_entry",
            "unknown",
        ],
        "judge_independently_of_governed_kind": (
            "The calibration question is whether the provider's suggested kind "
            "would be right, so judge the kind from the case content only. Do "
            "not copy the governed kind blindly, and do not label unknown "
            "merely because you disagree with the governed kind."
        ),
        "rules": {
            "fact": "A specific assertion about the world or system state that"
            " could in principle be checked against evidence.",
            "observation": "A recorded event or witnessed state at a point in"
            " time; a report of something seen happening, not a general claim.",
            "decision": "A recorded choice or course of action that was taken;"
            " it commits future behavior rather than describing the world.",
            "procedure": "Durable how-to knowledge: steps or instructions for performing a task.",
            "summary": "A condensed restatement of other material that adds no"
            " new independent claim.",
            "doctrine": "An organizational operating rule or standard that"
            " governs behavior; policy-like guidance rather than a world claim.",
            "invariant": "A hard constraint that must always hold; violating it"
            " is a defect (e.g. security invariants, schema guarantees).",
            "preference": "A stated like, dislike, or taste of an actor; not a"
            " claim about the world.",
            "diary_entry": "Personal journal content; part of the closed"
            " vocabulary though excluded from this campaign's sampling.",
            "unknown": "The kind is genuinely unresolved or custom. Reserve"
            " unknown for exactly that case.",
        },
    },
    "retention_value": {
        "allowed_vocabulary": ["retain", "do_not_retain", "uncertain"],
        "rules": (
            "Retention is durable usefulness for future memory-guided work —"
            " NEVER truth. A durable but unsupported procedure can be retain"
            " with weakly_supported. A correct greeting can be do_not_retain."
            " retain = durably useful; do_not_retain = not durably useful even"
            " if true; uncertain = usefulness cannot be judged."
        ),
    },
    "epistemic_state": {
        "allowed_vocabulary": [
            "adequately_supported",
            "weakly_supported",
            "contradicted",
            "contested",
            "ambiguous",
            "unverifiable",
            "unknown",
        ],
        "rules": (
            "Judge using evidence available AT DECISION TIME (the packet) only."
            " adequately_supported = attributable evidence supports the claim."
            " weakly_supported = support exists but is insufficient."
            " contradicted = available evidence opposes the claim."
            " contested = relevant evidence or reviewers support incompatible"
            " claims. ambiguous = the proposition permits materially different"
            " interpretations. unverifiable = no feasible verification method"
            " applies. unknown = the available record does not support a"
            " judgment. Never use later verification to rewrite decision-time"
            " support."
        ),
    },
    "consequence": {
        "allowed_vocabulary": ["low", "medium", "high", "unknown"],
        "rules": (
            "Consequence OF ERRONEOUS SILENT ADMISSION — what damage results if"
            " this memory is wrong and was silently admitted. low = small,"
            " reversible inconvenience. medium = material wasted work or an"
            " incorrect operational decision with feasible recovery. high ="
            " security invariants, organizational doctrine, destructive"
            " instructions, or claims that can cause broad or irreversible"
            " damage. unknown = cannot be assessed from the context. Never"
            " infer consequence from confidence, wording, or memory kind: a"
            " harmless test procedure can be low; a deletion procedure can be"
            " high."
        ),
    },
    "acceptable_abstention": {
        "allowed_vocabulary": ["yes", "no", "unknown"],
        "rules": (
            "Whether 'unknown' / no-score would be an ACCEPTABLE provider"
            " answer for this item. yes = the evidence, interpretation, or"
            " scope cannot support a responsible decision, so abstaining is"
            " appropriate. no = a responsible decision was possible from the"
            " packet, so abstention would lose signal. unknown = cannot"
            " assess. Blocking promotion is not automatically abstention: it"
            " can mean cooling, disabled policy, or a kind restriction."
        ),
    },
    "general_decision_rules": [
        "Judge from the decision-time evidence in the packet only.",
        "You never see provider scores, model suggestions, policy outputs, or"
        " other reviewers' labels; do not guess at them.",
        "Do not lower a dimension to make agreement look better.",
        "Do not label with the goal of matching what the memory system currently does.",
        "unknown is a legitimate, protected answer on every field; never guess to avoid it.",
    ],
}

# --- Frozen response contract -----------------------------------------------
# The model's ACTUAL response must be strict JSON exactly matching this shape.
# Those exact bytes are preserved; the judgment is parsed from them.
RESPONSE_CONTRACT: dict[str, Any] = {
    "response_schema": RESPONSE_SCHEMA_NAME,
    "strict_json": (
        "Respond with ONE strict JSON object and nothing else — no markdown"
        " fences, no prose before or after."
    ),
    "shape": {
        "sample_id": "string — echo the request's sample_id exactly",
        "outcome": '"judged" (you are supplying a judgment) or "refused"'
        " (you must decline this case)",
        "judgment": {
            "_required_when": "outcome == judged",
            "fields": (
                "object with EXACTLY these five required keys — expected_kind,"
                " retention_value, epistemic_state, consequence,"
                " acceptable_abstention — using only the allowed vocabulary"
                " above (optional diagnostic keys are permitted but ignored)"
            ),
            "reviewer_confidence": '"low" | "medium" | "high" — confidence in your own judgment',
        },
        "error_code": "short token, required when outcome == refused",
    },
}


def canonical_instruction_bundle(label_guide_version: str) -> dict[str, Any]:
    """The complete frozen instruction material supplied to reviewers.

    Everything the model receives semantically: task, guide identity, the
    #206 operationalization identity (FIX-R5-5), the full semantic bundle,
    and the strict response contract. ``prompt_digest`` binds exactly this
    object (via ``labeling_instructions_digest``).
    """
    return {
        "task": (
            "For each case, judge the memory item on the five critical fields "
            "(expected_kind, retention_value, epistemic_state, consequence, "
            "acceptable_abstention) using ONLY the semantics below."
        ),
        "label_guide_version": label_guide_version,
        "reviewer_instructions_version": REVIEWER_INSTRUCTIONS_VERSION,
        "semantics": CANONICAL_SEMANTIC_BUNDLE,
        "response_contract": RESPONSE_CONTRACT,
    }


# --- Deterministic response parser (FIX-R4-2) -------------------------------


class ParsedModelResponse(BaseModel):
    """Mechanical classification of preserved model-response bytes.

    ``classification`` is the DERIVED outcome: ``judged`` only when the bytes
    parse as strict JSON matching the frozen response contract and carry a
    valid judgment; ``refused`` only when the bytes explicitly refuse;
    ``malformed`` whenever the bytes fail any of that. The caller's
    assertions are never authority for the classification.
    """

    classification: Literal["judged", "refused", "malformed"]
    judgment: ModelJudgment | None = None
    error_code: str | None = None


def parse_model_response(raw: bytes, *, expected_sample_id: str) -> ParsedModelResponse:
    """Deterministically classify + parse the EXACT preserved response bytes.

    Fail-closed semantics:

    - bytes must be strict UTF-8 JSON (a JSON object, nothing else);
    - ``sample_id`` must equal the request's sample id (wrong-sample bytes are
      never accepted as evidence for this sample);
    - ``outcome`` must be ``judged`` or ``refused``;
    - ``judged`` requires a valid ``ModelJudgment`` under the frozen
      vocabulary (any violation degrades to ``malformed``);
    - ``refused`` requires an ``error_code``;
    - anything else — unparseable text, fences, prose, missing keys,
      out-of-vocabulary values — is ``malformed``.
    """
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ParsedModelResponse(classification="malformed")
    if not isinstance(payload, dict):
        return ParsedModelResponse(classification="malformed")
    if payload.get("sample_id") != expected_sample_id:
        # Response bytes that name another sample are not evidence for this
        # sample; ingest must refuse them outright (raised, not degraded).
        raise ValueError("response_sample_id_mismatch")
    outcome = payload.get("outcome")
    if outcome == "judged":
        judgment_payload = payload.get("judgment")
        if not isinstance(judgment_payload, dict):
            return ParsedModelResponse(classification="malformed")
        try:
            judgment = ModelJudgment.model_validate(judgment_payload)
        except Exception:
            return ParsedModelResponse(classification="malformed")
        if payload.get("error_code") is not None:
            return ParsedModelResponse(classification="malformed")
        return ParsedModelResponse(classification="judged", judgment=judgment)
    if outcome == "refused":
        error_code = payload.get("error_code")
        if not isinstance(error_code, str) or not error_code:
            return ParsedModelResponse(classification="malformed")
        if payload.get("judgment") is not None:
            return ParsedModelResponse(classification="malformed")
        return ParsedModelResponse(classification="refused", error_code=error_code)
    return ParsedModelResponse(classification="malformed")
