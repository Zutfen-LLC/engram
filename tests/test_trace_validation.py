"""Strict trace validation tests for ENG-AUDIT-001-FIX5.

Table-driven tests that remove or corrupt each required hook-trace field
individually and verify the validator fails closed. The governing rule is:

    missing binding evidence must fail exactly like mismatched binding evidence
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engram.memory_audit import RunState

# Load the CLI module (scripts/ is not a package).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "run_memory_e2e_audit.py"
_spec = importlib.util.spec_from_file_location("run_memory_e2e_audit", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
sys.modules["run_memory_e2e_audit"] = cli
_spec.loader.exec_module(cli)


# ── helpers ──────────────────────────────────────────────────────────────────


RECALL_PROMPT = "What is the controlled Engram recall marker?"
EPISTEMIC_PROMPT = "What color is the sky on February 30th?"


def _sha(prompt: str) -> str:
    return hashlib.sha256(
        prompt.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


RECALL_PROMPT_SHA = _sha(RECALL_PROMPT)
EPISTEMIC_PROMPT_SHA = _sha(EPISTEMIC_PROMPT)
QUERY_DIGEST = hashlib.sha256(RECALL_PROMPT.encode()).hexdigest()[:12]
SESSION_DIGEST = hashlib.sha256(b"test-session").hexdigest()[:12]


def _mkstate() -> RunState:
    return RunState(
        run_id=str(uuid.uuid4()),
        started_at=datetime.now(UTC),
        target_host="h",
    )


def _valid_record(
    item_id: str,
    *,
    fixture: str = "recall",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return a fully valid v2.0 hook-trace record."""
    prompt = RECALL_PROMPT if fixture == "recall" else EPISTEMIC_PROMPT
    record: dict[str, Any] = {
        "schema": "engram.hermes-hook-audit-trace",
        "schema_version": "2.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "hook": "pre_llm_call",
        "provider": "engram",
        "recall_enabled": True,
        "recall_succeeded": True,
        "recall_log_id": str(uuid.uuid4()),
        "retrieved_item_ids": [item_id],
        "injected_item_ids": [item_id],
        "retrieved_item_count": 1,
        "injected_item_count": 1,
        "native_memory_used": False,
        "error_code": None,
        "prompt_sha256": _sha(prompt),
        "query_digest": hashlib.sha256(prompt.encode()).hexdigest()[:12],
        "session_id_digest": SESSION_DIGEST,
        "turn_index": 1,
        "expected_prompt_sha256_match": True,
        "audit_fixture": fixture,
    }
    if run_id:
        record["audit_run_id"] = run_id
    return record


def _write_trace(tmp_path: Path, records: list[dict[str, Any]] | dict[str, Any]) -> Path:
    """Write one or more JSONL records to a trace file."""
    trace = tmp_path / "trace.jsonl"
    if isinstance(records, dict):
        records = [records]
    lines = [json.dumps(r) for r in records]
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return trace


def _validate(
    tmp_path: Path,
    record: dict[str, Any],
    *,
    fixture: str = "recall",
    run_id: str | None = None,
    item_id: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Convenience: write trace and validate."""
    rid = run_id or str(uuid.uuid4())
    iid = item_id or str(uuid.uuid4())
    # Inject audit_run_id if not present
    if "audit_run_id" not in record:
        record = {**record, "audit_run_id": rid}
    # Set item in lists if not present
    if iid not in record.get("retrieved_item_ids", []):
        record["retrieved_item_ids"] = [iid]
    if iid not in record.get("injected_item_ids", []):
        record["injected_item_ids"] = [iid]
    trace = _write_trace(tmp_path, record)
    return cli._validate_hook_trace(
        trace,
        expected_item_id=iid,
        expected_fixture=fixture,
        expected_run_id=rid,
    )


# ── baseline: valid trace passes ─────────────────────────────────────────────


def test_valid_recall_trace_passes(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    trace = _write_trace(tmp_path, rec)
    reason, evidence = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is None
    assert evidence["audit_run_id"] == rid
    assert evidence["audit_fixture"] == "recall"
    assert evidence["expected_prompt_sha256_match"] is True


def test_valid_epistemic_trace_passes(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="epistemic", run_id=rid)
    trace = _write_trace(tmp_path, rec)
    reason, evidence = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="epistemic", expected_run_id=rid
    )
    assert reason is None


# ── 1. Missing audit_run_id fails ────────────────────────────────────────────


def test_missing_audit_run_id_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["audit_run_id"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None


# ── 2. Invalid audit_run_id fails ────────────────────────────────────────────


def test_invalid_audit_run_id_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["audit_run_id"] = "not-a-uuid"
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None


# ── 3. Wrong run ID fails ────────────────────────────────────────────────────


def test_wrong_run_id_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=str(uuid.uuid4()))
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=str(uuid.uuid4())
    )
    assert reason == "HERMES_TRACE_RUN_MISMATCH"


# ── 4. Missing audit_fixture fails ───────────────────────────────────────────


def test_missing_audit_fixture_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["audit_fixture"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None


# ── 5. Wrong fixture fails ───────────────────────────────────────────────────


def test_wrong_fixture_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="epistemic", run_id=rid)
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_FIXTURE_MISMATCH"


# ── 6. Missing prompt_sha256 fails ───────────────────────────────────────────


def test_missing_prompt_sha256_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["prompt_sha256"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None


# ── 7. Wrong prompt hash fails ───────────────────────────────────────────────


def test_wrong_prompt_hash_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["prompt_sha256"] = "a" * 64
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_PROMPT_MISMATCH"


# ── 8. Missing expected_prompt_sha256_match fails ────────────────────────────


def test_missing_expected_prompt_match_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["expected_prompt_sha256_match"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_EXPECTED_PROMPT_UNPROVEN"


# ── 9. expected_prompt_sha256_match=false fails ──────────────────────────────


def test_expected_prompt_match_false_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["expected_prompt_sha256_match"] = False
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_EXPECTED_PROMPT_UNPROVEN"


# ── 10. error_code="audit_prompt_mismatch" fails ─────────────────────────────


def test_error_code_audit_prompt_mismatch_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["error_code"] = "audit_prompt_mismatch"
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_ERROR_PRESENT"


# ── 11. Any non-null error code fails ────────────────────────────────────────


@pytest.mark.parametrize(
    "error_code",
    ["recall_timeout", "embedding_failed", "connection_error", "", "null"],
)
def test_any_non_null_error_code_fails(tmp_path: Path, error_code: str) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["error_code"] = error_code
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_ERROR_PRESENT"


# ── 12. Missing query_digest fails ───────────────────────────────────────────


def test_missing_query_digest_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["query_digest"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_QUERY_UNPROVEN"


# ── 13. Missing session_id_digest fails ──────────────────────────────────────


def test_missing_session_id_digest_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["session_id_digest"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_SESSION_UNPROVEN"


# ── 14. Empty session_id_digest fails ────────────────────────────────────────


def test_empty_session_id_digest_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["session_id_digest"] = ""
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_SESSION_UNPROVEN"


# ── 15. Missing turn_index fails ─────────────────────────────────────────────


def test_missing_turn_index_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["turn_index"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_TURN_INVALID"


# ── 16. Turn index zero fails ────────────────────────────────────────────────


def test_turn_index_zero_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["turn_index"] = 0
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_TURN_INVALID"


# ── 17. Negative turn index fails ────────────────────────────────────────────


def test_negative_turn_index_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["turn_index"] = -1
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_TURN_INVALID"


# ── 18. String turn index fails ──────────────────────────────────────────────


def test_string_turn_index_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["turn_index"] = "1"
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_TURN_INVALID"


# ── 19. Missing recall_log_id fails ──────────────────────────────────────────


def test_missing_recall_log_id_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["recall_log_id"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_PROVENANCE_MISMATCH"


# ── 20. Retrieved-but-not-injected fails ─────────────────────────────────────


def test_retrieved_but_not_injected_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["retrieved_item_ids"] = [iid]
    rec["injected_item_ids"] = [other]
    rec["injected_item_count"] = 1
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_EXPECTED_ITEM_NOT_INJECTED"


# ── 21. Injected-but-not-retrieved fails ─────────────────────────────────────


def test_injected_but_not_retrieved_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["retrieved_item_ids"] = [other]
    rec["retrieved_item_count"] = 1
    rec["injected_item_ids"] = [iid]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_EXPECTED_ITEM_NOT_RETRIEVED"


# ── 22. Count/list mismatch fails ────────────────────────────────────────────


def test_count_list_mismatch_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["retrieved_item_count"] = 2  # but list has only 1
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_HOOK_TRACE_INVALID"


# ── 23. Two fully matching records fail as ambiguous ─────────────────────────


def test_two_matching_records_ambiguous(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec1 = _valid_record(iid, fixture="recall", run_id=rid)
    rec2 = _valid_record(iid, fixture="recall", run_id=rid)
    trace = _write_trace(tmp_path, [rec1, rec2])
    reason, evidence = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_AMBIGUOUS"
    assert evidence["matching_candidate_count"] == 2


# ── 24. One matching plus unrelated valid records passes ─────────────────────


def test_one_matching_plus_unrelated_passes(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    matching = _valid_record(iid, fixture="recall", run_id=rid)
    # An unrelated record with a different run_id and fixture
    unrelated = _valid_record(
        str(uuid.uuid4()), fixture="epistemic", run_id=str(uuid.uuid4())
    )
    trace = _write_trace(tmp_path, [unrelated, matching])
    reason, evidence = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is None


# ── 25. Malformed unrelated lines do not create success ─────────────────────


def test_malformed_lines_do_not_create_success(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    trace = tmp_path / "trace.jsonl"
    # Write a malformed line plus a record missing audit_run_id
    bad_json = "not-json-at-all"
    partial = _valid_record(iid, fixture="recall", run_id=rid)
    del partial["audit_run_id"]
    trace.write_text(bad_json + "\n" + json.dumps(partial) + "\n")
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None


# ── 26. Stage 5 rejects partially bound trace ────────────────────────────────


def test_stage_5_rejects_partially_bound_trace(tmp_path: Path) -> None:
    """A trace that has the right run+fixture but wrong prompt hash must fail
    Stage 5 (recall lane), not fall through to success."""
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["prompt_sha256"] = "b" * 64
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_PROMPT_MISMATCH"


# ── 27. Stage 6 rejects partially bound trace ───────────────────────────────


def test_stage_6_rejects_partially_bound_trace(tmp_path: Path) -> None:
    """A trace that has the right run+fixture but missing turn_index must
    fail Stage 6 (epistemic lane)."""
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="epistemic", run_id=rid)
    del rec["turn_index"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="epistemic", expected_run_id=rid
    )
    assert reason == "HERMES_TRACE_TURN_INVALID"


# ── Additional field-level corruption tests ──────────────────────────────────


@pytest.mark.parametrize(
    "field,corrupt_value,expected_reason",
    [
        ("schema", "wrong.schema", "HERMES_HOOK_TRACE_INVALID"),
        ("schema_version", "1.0", "HERMES_HOOK_TRACE_INVALID"),
        ("hook", "post_llm_call", "HERMES_HOOK_TRACE_INVALID"),
        ("provider", "openai", "HERMES_HOOK_TRACE_INVALID"),
        ("recall_enabled", False, "HERMES_HOOK_TRACE_INVALID"),
        ("recall_succeeded", False, "HERMES_HOOK_TRACE_INVALID"),
        ("native_memory_used", True, "HERMES_HOOK_TRACE_INVALID"),
        ("recall_enabled", None, "HERMES_HOOK_TRACE_INVALID"),
        ("recall_succeeded", None, "HERMES_HOOK_TRACE_INVALID"),
    ],
)
def test_field_corruption_fails(
    tmp_path: Path,
    field: str,
    corrupt_value: Any,
    expected_reason: str,
) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec[field] = corrupt_value
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason == expected_reason


# ── Missing-field table-driven tests ────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "schema_version",
        "hook",
        "provider",
        "recall_enabled",
        "recall_succeeded",
        "native_memory_used",
        "prompt_sha256",
        "query_digest",
        "session_id_digest",
        "turn_index",
        "recall_log_id",
        "retrieved_item_ids",
        "injected_item_ids",
        "retrieved_item_count",
        "injected_item_count",
        "expected_prompt_sha256_match",
    ],
)
def test_missing_field_fails(tmp_path: Path, field: str) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec[field]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None, f"missing {field} must fail"


# ── Missing error_code key treated as null (should pass) ────────────────────


def test_missing_error_code_passes(tmp_path: Path) -> None:
    """error_code missing is treated as null (key not required to be present
    as long as it's not a non-null value)."""
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    del rec["error_code"]
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    # error_code missing → record.get("error_code") returns None → should pass
    assert reason is None


# ── Fallback records never produce success ──────────────────────────────────


def test_fallback_record_never_produces_success(tmp_path: Path) -> None:
    """A structurally valid record with wrong run_id must not become a
    passing match even when it matches everything else."""
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    wrong_rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=wrong_rid)
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None
    assert reason == "HERMES_TRACE_RUN_MISMATCH"


# ── Empty file fails ────────────────────────────────────────────────────────


def test_empty_trace_file_fails(tmp_path: Path) -> None:
    trace = tmp_path / "empty.jsonl"
    trace.write_text("", encoding="utf-8")
    reason, _ = cli._validate_hook_trace(
        trace,
        expected_item_id=str(uuid.uuid4()),
        expected_fixture="recall",
        expected_run_id=str(uuid.uuid4()),
    )
    assert reason == "HERMES_HOOK_TRACE_INVALID"


# ── Nonexistent file fails ──────────────────────────────────────────────────


def test_nonexistent_trace_file_fails(tmp_path: Path) -> None:
    reason, _ = cli._validate_hook_trace(
        tmp_path / "nonexistent.jsonl",
        expected_item_id=str(uuid.uuid4()),
        expected_fixture="recall",
        expected_run_id=str(uuid.uuid4()),
    )
    assert reason == "HERMES_HOOK_TRACE_MISSING"


# ── prompt_sha256 uppercase hex fails (must be lowercase) ───────────────────


def test_uppercase_prompt_sha256_fails(tmp_path: Path) -> None:
    iid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rec = _valid_record(iid, fixture="recall", run_id=rid)
    rec["prompt_sha256"] = RECALL_PROMPT_SHA.upper()
    trace = _write_trace(tmp_path, rec)
    reason, _ = cli._validate_hook_trace(
        trace, expected_item_id=iid, expected_fixture="recall", expected_run_id=rid
    )
    assert reason is not None
