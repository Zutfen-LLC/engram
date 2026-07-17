"""Unit tests for engram/usage.py helpers that don't require a database.

Covers: defensive provider-usage extraction (chat + embedding shapes, missing
usage, malformed extras, cost in plausible locations), safe provider identity
derivation (hostname-only, never a path/query/credential), UTF-8 byte
counting, and the disabled-telemetry no-op path (must not open a DB session).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from engram.config import settings
from engram.usage import (
    ProviderUsage,
    _is_expected_unique_violation,
    embedding_call_occurred_for,
    extract_openai_compatible_usage,
    record_candidate_once,
    record_candidate_outcome,
    record_provider_call,
    record_retrieval_request,
    record_usage_event_best_effort,
    safe_provider_identity,
    utf8_byte_len,
)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("not_required", False),
        ("not_attempted", False),
        ("disabled", False),
        ("succeeded", True),
        ("failed", True),
        ("unknown", None),
    ],
)
def test_embedding_call_occurred_mapping(outcome, expected):
    assert embedding_call_occurred_for(outcome) is expected


async def test_unknown_retrieval_outcome_records_explicit_null_boolean(monkeypatch):
    captured: dict[str, object] = {}

    async def capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("engram.usage.record_usage_event_best_effort", capture)
    await record_retrieval_request(
        tenant_id="00000000-0000-0000-0000-000000000001",
        principal_id=None,
        workspace_id=None,
        operation="semantic_recall",
        status="failed",
        embedding_outcome="unknown",
    )
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["embedding_outcome"] == "unknown"
    assert "embedding_call_occurred" in metadata
    assert metadata["embedding_call_occurred"] is None


async def test_legacy_retrieval_without_embedding_fields_preserves_absence(monkeypatch):
    captured: dict[str, object] = {}

    async def capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("engram.usage.record_usage_event_best_effort", capture)
    await record_retrieval_request(
        tenant_id="00000000-0000-0000-0000-000000000001",
        principal_id=None,
        workspace_id=None,
        operation="startup_recall",
        status="succeeded",
    )
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert "embedding_outcome" not in metadata
    assert "embedding_call_occurred" not in metadata


async def test_retrieval_metadata_carries_only_bounded_memory_context_provenance(monkeypatch):
    captured: dict[str, object] = {}

    async def capture(**kwargs: object) -> None:
        captured.update(kwargs)

    profile_id, revision_id = uuid4(), uuid4()
    monkeypatch.setattr("engram.usage.record_usage_event_best_effort", capture)
    await record_retrieval_request(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        workspace_id=None,
        operation="hybrid_search",
        status="succeeded",
        memory_context_version="memory-context-v1",
        memory_profile_id=profile_id,
        memory_profile_revision_id=revision_id,
        memory_profile_version=7,
    )
    metadata = captured["metadata"]
    assert metadata == {
        "memory_context_version": "memory-context-v1",
        "memory_profile_id": str(profile_id),
        "memory_profile_revision_id": str(revision_id),
        "memory_profile_version": 7,
    }
    assert "query" not in str(metadata).lower()
    assert "content" not in str(metadata).lower()
    assert "workspace_ids" not in str(metadata).lower()


def test_utf8_byte_len_counts_bytes_not_characters():
    # "café" is 4 Python characters but 5 UTF-8 bytes (é is 2 bytes).
    assert utf8_byte_len("café") == 5
    assert utf8_byte_len("") == 0
    # Emoji: 1 character, 4 UTF-8 bytes.
    assert utf8_byte_len("🎉") == 4


def test_extract_usage_chat_completion_shape():
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    )
    usage = extract_openai_compatible_usage(response)
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 20
    assert usage.total_tokens == 120
    assert usage.reported_cost_usd is None


def test_extract_usage_embedding_shape_no_completion_tokens():
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=42, total_tokens=42))
    usage = extract_openai_compatible_usage(response)
    assert usage.prompt_tokens == 42
    assert usage.completion_tokens is None
    assert usage.total_tokens == 42


def test_extract_usage_missing_usage_is_valid_not_an_error():
    response = SimpleNamespace()
    usage = extract_openai_compatible_usage(response)
    assert usage == ProviderUsage()
    assert usage.prompt_tokens is None
    assert usage.total_tokens is None
    assert usage.reported_cost_usd is None


def test_extract_usage_top_level_cost_captured_even_when_usage_absent():
    """Some OpenAI-compatible providers attach cost only to the top-level
    response (never on a ``usage`` object). Missing usage must not prevent
    capturing a top-level cost (ENG-METER-001 correction)."""
    response = SimpleNamespace(total_cost=0.015)
    usage = extract_openai_compatible_usage(response)
    assert usage.prompt_tokens is None
    assert usage.total_tokens is None
    assert usage.reported_cost_usd == pytest.approx(0.015)


def test_extract_usage_malformed_extras_never_raises():
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens="not-a-number", cost="oops"))
    usage = extract_openai_compatible_usage(response)
    assert usage.prompt_tokens is None
    assert usage.reported_cost_usd is None


@pytest.mark.parametrize("cost_field", ["cost", "total_cost", "estimated_cost", "cost_usd"])
def test_extract_usage_cost_in_plausible_usage_locations(cost_field):
    kwargs = {"prompt_tokens": 10, "total_tokens": 10, cost_field: 0.0042}
    response = SimpleNamespace(usage=SimpleNamespace(**kwargs))
    usage = extract_openai_compatible_usage(response)
    assert usage.reported_cost_usd == pytest.approx(0.0042)


def test_extract_usage_cost_on_top_level_response():
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, total_tokens=10), cost=0.01)
    usage = extract_openai_compatible_usage(response)
    assert usage.reported_cost_usd == pytest.approx(0.01)


def test_extract_usage_cost_via_model_extra():
    # Pydantic v2 BaseModel with extra="allow" surfaces unknown fields here.
    usage_obj = SimpleNamespace(
        prompt_tokens=10, total_tokens=10, model_extra={"estimated_cost": 0.002}
    )
    response = SimpleNamespace(usage=usage_obj)
    usage = extract_openai_compatible_usage(response)
    assert usage.reported_cost_usd == pytest.approx(0.002)


def test_safe_provider_identity_bare_hostname_only():
    adapter, host = safe_provider_identity("openai", "https://api.deepinfra.com/v1/openai")
    assert adapter == "openai"
    assert host == "api.deepinfra.com"


def test_safe_provider_identity_never_leaks_path_query_or_credentials():
    adapter, host = safe_provider_identity(
        "openai", "https://user:secret@sneaky.example.com/v1/openai?api_key=leak"
    )
    assert host == "sneaky.example.com"
    assert "user" not in (host or "")
    assert "secret" not in (host or "")
    assert "api_key" not in (host or "")
    assert "?" not in (host or "")
    assert "/v1" not in (host or "")


def test_safe_provider_identity_openai_default_host_when_unset():
    adapter, host = safe_provider_identity("openai", None)
    assert adapter == "openai"
    assert host == "api.openai.com"


def test_safe_provider_identity_unknown_adapter_no_default():
    adapter, host = safe_provider_identity("local", None)
    assert adapter == "local"
    assert host is None


def test_safe_provider_identity_malformed_url_never_raises():
    adapter, host = safe_provider_identity("openai", "not a url at all :::")
    assert adapter == "openai"
    # host may be None or a best-effort parse; must not raise.


async def test_disabled_telemetry_is_noop_and_opens_no_db_session(monkeypatch):
    """When ENGRAM_USAGE_TELEMETRY_ENABLED is false, helpers must be cheap
    no-ops that never touch engram.db (asserted by monkeypatching
    async_session_factory to raise if called).
    """
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not open a DB session when telemetry is disabled")

    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _boom)

    result = await record_usage_event_best_effort(
        tenant_id="00000000-0000-0000-0000-000000000001",
        event_type="candidate.observed",
        operation="process_memory_candidate",
        status="accepted_for_processing",
    )
    assert result is None

    result2 = await record_candidate_once(
        tenant_id="00000000-0000-0000-0000-000000000001",
        principal_id=None,
        workspace_id=None,
        correlation_id=__import__("uuid").uuid4(),
        ingest_id=__import__("uuid").uuid4(),
        candidate_utf8_bytes=10,
        source_type="manual",
    )
    assert result2 is None

    result3 = await record_provider_call(
        tenant_id="00000000-0000-0000-0000-000000000001",
        operation="classification",
        status="succeeded",
        usage_class="request",
        external_call_attempted=True,
    )
    assert result3 is None


async def test_unresolvable_tenant_id_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    result = await record_usage_event_best_effort(
        tenant_id="not-a-uuid",
        event_type="candidate.observed",
        operation="process_memory_candidate",
        status="accepted_for_processing",
    )
    assert result is None


async def test_candidate_outcome_reuses_attempt_id_for_telemetry_retry(monkeypatch):
    from uuid import uuid4

    captured: list[dict[str, object]] = []

    async def capture(**kwargs: object):
        captured.append(kwargs)
        return kwargs["event_id"]

    monkeypatch.setattr("engram.usage.record_usage_event_best_effort", capture)
    attempt_id = uuid4()
    ingest_id = uuid4()
    correlation_id = uuid4()
    for _ in range(2):
        await record_candidate_outcome(
            tenant_id=uuid4(),
            principal_id=None,
            workspace_id=None,
            correlation_id=correlation_id,
            ingest_id=ingest_id,
            attempt_id=attempt_id,
            status="created",
        )
    assert [call["event_id"] for call in captured] == [attempt_id, attempt_id]
    assert all(call["ingest_id"] == ingest_id for call in captured)


# ---- IntegrityError discrimination (ENG-METER-001 correction) ----
#
# record_usage_event_best_effort must suppress ONLY the intended idempotent
# unique-violations (duplicate PK or dedupe_key); every other integrity failure
# (FK, CHECK, privilege error surfaced as integrity) must be logged distinctly,
# not hidden as a "duplicate".


def _fake_integrity_error(*, sqlstate: str | None, constraint: str | None):
    """Build an IntegrityError-shaped object that mirrors how SQLAlchemy wraps
    an asyncpg native exception (see engram.api.errors._dbapi_exc)."""
    native = SimpleNamespace(sqlstate=sqlstate, constraint_name=constraint)
    wrapped_orig = SimpleNamespace(__cause__=native)
    # IntegrityError carries the wrapper on .orig
    return SimpleNamespace(orig=wrapped_orig)


def test_expected_unique_violation_on_primary_key_is_suppressed():
    exc = _fake_integrity_error(sqlstate="23505", constraint="usage_events_pkey")
    assert _is_expected_unique_violation(exc) is True


def test_expected_unique_violation_on_dedupe_index_is_suppressed():
    exc = _fake_integrity_error(sqlstate="23505", constraint="idx_usage_events_dedupe")
    assert _is_expected_unique_violation(exc) is True


def test_foreign_key_violation_is_not_suppressed():
    # 23503 = foreign_key_violation — a real telemetry failure (e.g. a stale
    # tenant/principal reference), never an idempotent duplicate.
    exc = _fake_integrity_error(sqlstate="23503", constraint="usage_events_tenant_id_fkey")
    assert _is_expected_unique_violation(exc) is False


def test_check_violation_is_not_suppressed():
    # 23514 = check_violation — e.g. a negative token count or unresolvable
    # status, never an idempotent duplicate.
    exc = _fake_integrity_error(
        sqlstate="23514", constraint="chk_usage_events_input_count_nonneg"
    )
    assert _is_expected_unique_violation(exc) is False


def test_unique_violation_on_unrelated_constraint_is_not_suppressed():
    # A unique violation (23505) on a DIFFERENT constraint than the two we
    # expect is still a real failure — the whitelist must not over-match.
    exc = _fake_integrity_error(sqlstate="23505", constraint="some_other_unique_index")
    assert _is_expected_unique_violation(exc) is False


def test_integrity_error_without_sqlstate_is_not_suppressed():
    # When SQLSTATE can't be determined (non-PostgreSQL backend, malformed
    # exception), do not guess — treat it as a real failure.
    exc = _fake_integrity_error(sqlstate=None, constraint="usage_events_pkey")
    assert _is_expected_unique_violation(exc) is False
