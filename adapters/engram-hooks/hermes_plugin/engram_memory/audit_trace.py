"""Opt-in structured audit trace sink for the Engram Hermes hook.

When the environment variable ``ENGRAM_HOOKS_AUDIT_TRACE_FILE`` is set,
``emit_audit_trace`` appends one sanitized JSON Lines record per
``pre_llm_call`` hook execution. The record is deliberately minimal and
provenance-oriented: it carries only item IDs, counts, and categorical
dispositions — never API keys, database URLs, raw memory content, or raw
exception text.

When the optional audit-binding environment variables are set
(``ENGRAM_HOOKS_AUDIT_RUN_ID``, ``ENGRAM_HOOKS_AUDIT_FIXTURE``,
``ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256``), the trace record additionally
binds the hook execution to the exact audit run, fixture lane, and prompt.
The adapter computes the actual prompt hash from the query received by
``pre_llm_call`` — it does NOT blindly trust a caller-supplied hash.

The sink is always disabled when the trace-file variable is unset, and it
fails open: any I/O or serialization error is swallowed so that audit tracing
can never change recall behavior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_TRACE_ENV_VAR = "ENGRAM_HOOKS_AUDIT_TRACE_FILE"
_SCHEMA = "engram.hermes-hook-audit-trace"
_SCHEMA_VERSION = "2.0"

# Audit-only metadata environment variables (never affect recall behavior).
_AUDIT_RUN_ID_ENV = "ENGRAM_HOOKS_AUDIT_RUN_ID"
_AUDIT_FIXTURE_ENV = "ENGRAM_HOOKS_AUDIT_FIXTURE"
_AUDIT_EXPECTED_PROMPT_SHA256_ENV = "ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256"

_ALLOWED_FIXTURES = frozenset({"recall", "epistemic"})

# Guard rail: never allow these to appear in a trace record even if a future
# caller accidentally passes them through ``outcome_data``.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_header",
        "token",
        "secret",
        "password",
        "base_url",
        "database_url",
        "db_url",
        "content",
        "raw_content",
        "body",
        "environ",
        "env",
        "headers",
    }
)

# Regex: 64 lowercase hex characters (SHA-256).
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def audit_prompt_sha256(prompt: str) -> str:
    """Compute the canonical SHA-256 hash of a prompt.

    Normalization:
    1. convert CRLF/CR to LF;
    2. remove no content;
    3. add no implicit trailing newline.

    The hash is always 64 lowercase hex characters.
    """
    normalized = prompt.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sanitize_profile(value: object) -> str | None:
    """Reduce a profile name to a short, alphanumeric-ish label or None.

    We accept only simple identifiers so a stray path or secret-looking value
    cannot leak into the trace.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 64:
        cleaned = cleaned[:64]
    # Keep it simple: drop anything outside a safe display set.
    safe = "".join(
        ch if (ch.isalnum() or ch in {"-", "_", "."}) else "_" for ch in cleaned
    )
    return safe or None


def _coerce_id_list(value: object) -> list[str]:
    """Coerce a candidate value into a bounded list of short string IDs."""
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for entry in value[:200]:
        if not isinstance(entry, str):
            continue
        text = entry.strip()[:256]
        if text:
            result.append(text)
    return result


def _read_audit_metadata() -> dict[str, object]:
    """Read and validate audit-only environment variables.

    Returns a dict with keys:
      ``audit_run_id``: validated UUID string, or None
      ``audit_fixture``: ``"recall"`` or ``"epistemic"``, or None
      ``expected_prompt_sha256``: validated 64-hex string, or None

    Malformed values cause the corresponding key to be omitted (the trace
    record records a validation error instead). This never alters recall.
    """
    result: dict[str, object] = {}
    run_id_raw = os.environ.get(_AUDIT_RUN_ID_ENV, "").strip()
    if run_id_raw:
        try:
            result["audit_run_id"] = str(uuid.UUID(run_id_raw))
        except ValueError:
            result["audit_run_id_error"] = "invalid_uuid"

    fixture_raw = os.environ.get(_AUDIT_FIXTURE_ENV, "").strip()
    if fixture_raw:
        if fixture_raw in _ALLOWED_FIXTURES:
            result["audit_fixture"] = fixture_raw
        else:
            result["audit_fixture_error"] = "invalid_fixture"

    expected_hash_raw = os.environ.get(
        _AUDIT_EXPECTED_PROMPT_SHA256_ENV, ""
    ).strip()
    if expected_hash_raw:
        if _HEX64_RE.match(expected_hash_raw):
            result["expected_prompt_sha256"] = expected_hash_raw
        else:
            result["expected_prompt_sha256_error"] = "invalid_hash_format"

    return result


def _build_record(outcome_data: dict[str, object]) -> dict[str, object]:
    """Build the sanitized trace record from raw outcome data."""
    retrieved = _coerce_id_list(outcome_data.get("retrieved_item_ids"))
    injected = _coerce_id_list(outcome_data.get("injected_item_ids"))
    recall_succeeded = bool(outcome_data.get("recall_succeeded"))
    error_code_raw = outcome_data.get("error_code")
    error_code = str(error_code_raw) if isinstance(error_code_raw, str) else None
    recall_log_id_raw = outcome_data.get("recall_log_id")
    recall_log_id = (
        recall_log_id_raw.strip()[:256]
        if isinstance(recall_log_id_raw, str)
        else None
    )
    profile = _sanitize_profile(outcome_data.get("profile"))

    record: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "hook": "pre_llm_call",
        "provider": "engram",
        "profile": profile,
        "recall_enabled": True,
        "recall_succeeded": recall_succeeded,
        "recall_log_id": recall_log_id,
        "retrieved_item_ids": retrieved,
        "injected_item_ids": injected,
        "retrieved_item_count": len(retrieved),
        "injected_item_count": len(injected),
        "native_memory_used": False,
        "error_code": error_code,
    }

    # ── Audit binding fields ────────────────────────────────────────────
    # Compute actual prompt hash from the query received by pre_llm_call.
    query_raw = outcome_data.get("query")
    if isinstance(query_raw, str) and query_raw:
        prompt_hash = audit_prompt_sha256(query_raw)
        record["prompt_sha256"] = prompt_hash
        record["query_digest"] = hashlib.sha256(
            query_raw.encode("utf-8")
        ).hexdigest()[:12]

    # Session digest (derived from the actual Hermes session ID).
    session_id_raw = outcome_data.get("session_id")
    if isinstance(session_id_raw, str) and session_id_raw:
        record["session_id_digest"] = hashlib.sha256(
            session_id_raw.encode("utf-8")
        ).hexdigest()[:12]

    # Turn index (the actual hook turn index).
    turn_index_raw = outcome_data.get("turn_index")
    if isinstance(turn_index_raw, int) and turn_index_raw >= 0:
        record["turn_index"] = turn_index_raw

    # Read audit-only metadata from the environment.
    audit_meta = _read_audit_metadata()

    # If expected_prompt_sha256 was provided AND we computed the actual hash,
    # record whether they match. A mismatch is a categorical trace error.
    expected_hash = audit_meta.pop("expected_prompt_sha256", None)
    actual_hash = record.get("prompt_sha256")
    if expected_hash is not None and actual_hash is not None:
        match = expected_hash == actual_hash
        record["expected_prompt_sha256_match"] = match
        if not match:
            record["error_code"] = "audit_prompt_mismatch"

    # Merge remaining audit metadata (run_id, fixture).
    record.update(audit_meta)

    return record


def _record_is_clean(record: dict[str, object]) -> bool:
    """Defensive last-chance check that no secret key slipped into the record."""
    return all(key.lower() not in _SECRET_KEYS for key in record)


def emit_audit_trace(outcome_data: dict[str, object]) -> None:
    """Append one sanitized JSON Lines trace record, or no-op.

    Disabled when ``ENGRAM_HOOKS_AUDIT_TRACE_FILE`` is unset/empty. Any failure
    (serialization, permission, disk) is logged at debug level and swallowed
    so recall behavior is never affected.
    """
    trace_path = os.environ.get(_TRACE_ENV_VAR, "")
    if not trace_path:
        return
    try:
        record = _build_record(outcome_data)
        if not _record_is_clean(record):
            logger.debug("audit trace record failed secret-key hygiene; skipping")
            return
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        # Open for append; create with 0600 if it does not exist. We set the
        # mode on creation via os.open to guarantee it even on filesystems
        # whose default umask would have produced a looser mode.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(trace_path, flags, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - audit trace must fail open
        logger.debug("audit trace write failed (fail open)", exc_info=True)
