"""Opt-in structured audit trace sink for the Engram Hermes hook.

When the environment variable ``ENGRAM_HOOKS_AUDIT_TRACE_FILE`` is set,
``emit_audit_trace`` appends one sanitized JSON Lines record per
``pre_llm_call`` hook execution. The record is deliberately minimal and
provenance-oriented: it carries only item IDs, counts, and categorical
dispositions — never API keys, database URLs, raw memory content, or raw
exception text.

The sink is always disabled when the variable is unset, and it fails open:
any I/O or serialization error is swallowed so that audit tracing can never
change recall behavior.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_TRACE_ENV_VAR = "ENGRAM_HOOKS_AUDIT_TRACE_FILE"
_SCHEMA = "engram.hermes-hook-audit-trace"
_SCHEMA_VERSION = "1.0"

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
    return {
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
