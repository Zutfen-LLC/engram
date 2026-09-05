"""#162C raw blind-review provenance normalization.

Raw Markdown is immutable primary evidence.  This module maps its deliberately
compact rubric line into the formal label dimensions without consulting policy
or candidate modules.  It fails closed on any membership, vocabulary, or
blindness violation and writes only to protected paths outside the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from evals.admission.schema import Dimensions, HumanJudgment, digest

_COMPACT = re.compile(
    r"`(?P<retention>retain|do_not_retain)\s*/\s*"
    r"(?P<epistemic>[a-z_]+)\s*/\s*(?P<consequence>low|medium|high)\s*/\s*"
    r"disposition=(?P<storage>retain|defer|reject)\s*/\s*startup=(?P<startup>yes|no)\s*/\s*"
    r"governed=(?P<governed>yes|no)\s*/\s*review=(?P<review>yes|no)\s*/\s*"
    r"kind=(?P<kind>[a-z_]+)`"
)
_FULL_HEADER = re.compile(
    r"^(?:###\s*Case\s+(?P<c_number>\d+)\s+—\s+`(?P<c_id>rvw_[0-9a-f]{24})`|"
    r"\*\*Case\s+(?P<d_number>\d+)\s*/\s*(?P<d_id>rvw_[0-9a-f]{24})\*\*)\s*$",
    re.MULTILINE,
)
_SUBSET_HEADER = re.compile(r"^(?P<number>\d+)\.\s+", re.MULTILINE)
_POLICY = re.compile(
    r"\b(?:current[- ]policy|candidate[- ]policy|P[0-3]\s+(?:predicted|output|result)|"
    r"candidate-(?:current|tier|evidence|kind))\b",
    re.IGNORECASE,
)


def _packet_ids(packet: Mapping[str, Any]) -> list[str]:
    cases = packet.get("cases")
    if not isinstance(cases, list):
        raise ValueError("invalid_blind_packet")
    ids = [case.get("review_case_id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or any(not isinstance(value, str) for value in ids):
        raise ValueError("invalid_blind_packet")
    return cast(list[str], ids)


def _sections(raw: str, reviewer: str) -> list[tuple[int, str | None, str]]:
    if reviewer in {"c", "d"}:
        matches = list(_FULL_HEADER.finditer(raw))
        result: list[tuple[int, str | None, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            number = int(match.group("c_number") or match.group("d_number"))
            review_id = match.group("c_id") or match.group("d_id")
            result.append((number, review_id, raw[match.end() : end].strip()))
        return result
    matches = list(_SUBSET_HEADER.finditer(raw))
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        result.append((int(match.group("number")), None, raw[match.end() : end].strip()))
    return result


def _dimensions(match: re.Match[str], notes: str) -> dict[str, Any]:
    values = match.groupdict()
    epistemic_values = {
        "adequately_supported",
        "weakly_supported",
        "contradicted",
        "contested",
        "ambiguous",
        "unverifiable",
        "unknown",
    }
    kind_values = {
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
    }
    if values["epistemic"] not in epistemic_values or values["kind"] not in kind_values:
        raise ValueError("reviewer_judgment_unrepresentable")
    flags = re.search(r"(?:\*\*)?Flags(?:\*\*)?\s*:\s*([^\n]+)", notes, re.IGNORECASE)
    flag_text = flags.group(1).lower() if flags else ""
    temporal = "yes" if "temporal/outdated" in flag_text else "unknown"
    scope = "yes" if "scope concern" in flag_text else "unknown"
    supersession = "yes" if "supersession" in flag_text else "unknown"
    needs_review = values["storage"] == "defer" or values["review"] == "yes"
    action = "reject" if values["storage"] == "reject" else (
        "review" if needs_review else "automatic_admission"
    )
    dimensions = {
        "atomic": "unknown",
        "proposition_count": "unknown",
        "attribution": "unavailable",
        "source_span": "unavailable",
        "evidence_span": "unavailable",
        "assertion_origin": "unknown",
        "expected_kind": values["kind"],
        "expected_subject_or_domain": "unknown",
        "expected_scope": "unknown",
        "retention_value": values["retention"],
        "epistemic_state": values["epistemic"],
        "factual_outcome": None,
        "consequence": values["consequence"],
        "expected_storage_disposition": values["storage"],
        "expected_startup_eligibility": values["startup"],
        "expected_governed_semantic_eligibility": values["governed"],
        "human_review_required": values["review"],
        "acceptable_abstention": "unknown",
        "conflict_expected": "unknown",
        "dispute_expected": "unknown",
        "supersession_expected": supersession,
        "temporal_validity_issue": temporal,
        "scope_visibility_concern": scope,
        "evidence_independence": "unknown",
        "expected_blockers": None,
        "expected_next_action": action,
    }
    return Dimensions.model_validate(dimensions).model_dump(mode="json")


def normalize_raw_review(
    *, reviewer: str, raw: bytes, packet: Mapping[str, Any], expected_case_numbers: Sequence[int],
    normalized_at: datetime | None = None,
) -> dict[str, Any]:
    """Normalize one raw B/C/D review without consulting any other reviewer."""
    if reviewer not in {"b", "c", "d"}:
        raise ValueError("invalid_reviewer")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reviewer_source_not_utf8") from exc
    # The provenance attestation itself must say that outputs were not visible;
    # exclude only that attestation from contamination scanning.
    scannable = "\n".join(
        line
        for line in text.splitlines()
        if not ("blind to" in line.lower() and "candidate" in line.lower())
    )
    if _POLICY.search(scannable):
        raise ValueError("reviewer_source_policy_contamination")
    packet_ids = _packet_ids(packet)
    expected = tuple(expected_case_numbers)
    sections = _sections(text, reviewer)
    mapped: list[tuple[int, str, str]]
    if reviewer == "b":
        source_numbers = tuple(number for number, _, _ in sections)
        if source_numbers != tuple(range(1, len(expected) + 1)):
            raise ValueError("reviewer_case_membership_mismatch")
        mapped = [
            (expected[index], packet_ids[expected[index] - 1], body)
            for index, (_, _, body) in enumerate(sections)
        ]
    else:
        if any(review_id is None for _, review_id, _ in sections):
            raise ValueError("reviewer_case_membership_mismatch")
        mapped = [(number, cast(str, review_id), body) for number, review_id, body in sections]
    if tuple(number for number, _, _ in mapped) != expected:
        raise ValueError("reviewer_case_membership_mismatch")
    if any(review_id != packet_ids[number - 1] for number, review_id, _ in mapped):
        raise ValueError("reviewer_case_membership_mismatch")
    when = normalized_at or datetime(2026, 9, 6, tzinfo=UTC)
    if when.tzinfo is None:
        raise ValueError("aware_normalized_at_required")
    records: list[dict[str, Any]] = []
    for number, review_id, body in mapped:
        compact = _COMPACT.search(body)
        if compact is None or len(_COMPACT.findall(body)) != 1:
            raise ValueError("reviewer_judgment_unrepresentable")
        dimensions = _dimensions(compact, body)
        notes = (body[: compact.start()] + body[compact.end() :]).strip()
        judgment = {
            "adjudicator_ref": f"reviewer_{reviewer}",
            "adjudicated_at": when.isoformat(),
            "adjudicator_confidence": "unknown",
            "reason_code": f"independent_blind_reviewer_{reviewer}",
            "dimensions": dimensions,
            "usefulness": None,
        }
        HumanJudgment.model_validate(judgment)
        records.append({
            "case": number,
            "review_case_id": review_id,
            "normalized": judgment,
            "original_compact_judgment": compact.group(0),
            "original_notes": notes,
        })
    artifact = {
        "artifact_schema_version": "engram-162c-normalized-raw-review-v1",
        "reviewer": reviewer,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_bytes": len(raw),
        "source_policy_blind": True,
        "source_candidate_outputs_visible": False,
        "normalization": {
            "normalizer_version": "engram-162c-raw-review-normalizer-v1",
            "format": "compact_markdown_rubric_v1",
            "substantive_mapping": "verbatim compact judgment; notes preserved separately",
        },
        "normalized_at": when.isoformat(),
        "records": records,
    }
    artifact["normalized_digest"] = digest(artifact)
    return artifact


def write_private_provenance(artifact: Mapping[str, Any], path: Path) -> str:
    """Write a canonical private derived artifact with mode 0600."""
    repo = Path(__file__).resolve().parents[3]
    if path.resolve().is_relative_to(repo):
        raise ValueError("private_output_must_be_outside_repository")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    return path.name


def finalize_multireviewer_corpus(
    *,
    reviewer_a: Mapping[str, Any],
    reviewer_b: Mapping[str, Any],
    reviewer_c: Mapping[str, Any],
    reviewer_d: Mapping[str, Any],
    overrides: Mapping[int, Mapping[str, Any]],
    frozen_at: datetime,
) -> dict[str, Any]:
    """Seal #162C with C/D as panel, B subset evidence, and explicit finality."""
    if frozen_at.tzinfo is None:
        raise ValueError("aware_frozen_at_required")
    a_by_case = {int(record["case"]): record for record in reviewer_a["records"]}
    b_by_case = {int(record["case"]): record for record in reviewer_b["records"]}
    c_by_case = {int(record["case"]): record for record in reviewer_c["records"]}
    d_by_case = {int(record["case"]): record for record in reviewer_d["records"]}
    expected = set(range(1, 31))
    if set(a_by_case) != expected or set(c_by_case) != expected or set(d_by_case) != expected:
        raise ValueError("final_corpus_membership_mismatch")
    if set(b_by_case) != {2, 6, 7, 8, 12, 21, 22, 24}:
        raise ValueError("reviewer_b_membership_mismatch")
    if set(overrides) - expected:
        raise ValueError("adjudication_membership_mismatch")
    records: list[dict[str, Any]] = []
    adjudications: list[dict[str, Any]] = []
    for case in range(1, 31):
        c = c_by_case[case]["normalized"]
        d = d_by_case[case]["normalized"]
        dimensions = dict(c["dimensions"])
        dimensions.update(overrides.get(case, {}))
        final = {
            "adjudicator_ref": "operator_final_adjudication",
            "adjudicated_at": frozen_at.isoformat(),
            "adjudicator_confidence": "high",
            "reason_code": "operator_ratified_multireviewer_resolution",
            "dimensions": Dimensions.model_validate(dimensions).model_dump(mode="json"),
            "usefulness": None,
        }
        HumanJudgment.model_validate(final)
        a_label = a_by_case[case]["label"]
        label = {
            **a_label,
            "reviewer_b": b_by_case.get(case, {}).get("normalized"),
            "reviewer_c": c,
            "reviewer_d": d,
            "resolution": final,
            "disagreement": "resolved",
            "review_stage": "complete",
        }
        # C/D fields are deliberately held in this v2 private corpus rather
        # than changing the accepted v1 label schema and its baseline digest.
        # Every final judgment and dimension is validated above by Pydantic.
        records.append({"case": case, "review_case_id": a_label["sample_id"], "label": label})
        final_dimensions = cast(dict[str, Any], final["dimensions"])
        adjudications.append(
            {
                "case": case,
                "review_case_id": a_label["sample_id"],
                "final": final,
                "departure_fields": sorted(
                    field
                    for field, value in final_dimensions.items()
                    if value != c["dimensions"].get(field)
                    or value != d["dimensions"].get(field)
                ),
                "reason": "operator-ratified explicit resolution; no mechanical majority vote",
            }
        )
    adjudication = {
        "artifact_schema_version": "engram-162c-final-adjudication-v1",
        "policy_blind": True,
        "candidate_outputs_visible": False,
        "consensus_doctrine": {
            "primary_panel": "C/D independent full-holdout panel",
            "reviewer_b": "additional independent evidence for frozen-A high subset",
            "reviewer_a": "provenance only; not a mechanical tie-breaker",
            "mechanical_majority_vote": False,
        },
        "records": adjudications,
    }
    adjudication["adjudication_digest"] = digest(adjudication)
    corpus = {
        "artifact_schema_version": "engram-final-human-corpus-v2",
        "reviewer_a_frozen_digest": reviewer_a["frozen_digest"],
        "reviewer_b_normalized_digest": reviewer_b["normalized_digest"],
        "reviewer_c_normalized_digest": reviewer_c["normalized_digest"],
        "reviewer_d_normalized_digest": reviewer_d["normalized_digest"],
        "adjudication_digest": adjudication["adjudication_digest"],
        "frozen_at": frozen_at.isoformat(),
        "records": records,
        "summary": {
            "case_count": 30,
            "final_valid_label_count": 30,
            "unresolved_disagreement_count": 0,
            "high_consequence_without_b_count": sum(
                record["label"]["reviewer_a"]["dimensions"]["consequence"] == "high"
                and record["label"]["reviewer_b"] is None
                for record in records
            ),
            "reviewer_counts": {"a": 30, "b": 8, "c": 30, "d": 30},
        },
    }
    if corpus["summary"]["high_consequence_without_b_count"]:
        raise ValueError("high_consequence_without_b")
    corpus["final_corpus_digest"] = digest(corpus)
    return {"adjudication": adjudication, "corpus": corpus}
