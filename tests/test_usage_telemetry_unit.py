"""Unit tests for engram/usage.py helpers that don't require a database.

Covers: defensive provider-usage extraction (chat + embedding shapes, missing
usage, malformed extras, cost in plausible locations), safe provider identity
derivation (hostname-only, never a path/query/credential), UTF-8 byte
counting, and the disabled-telemetry no-op path (must not open a DB session).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engram.config import settings
from engram.usage import (
    ProviderUsage,
    extract_openai_compatible_usage,
    record_candidate_once,
    record_provider_call,
    record_usage_event_best_effort,
    safe_provider_identity,
    utf8_byte_len,
)


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
        candidate_utf8_bytes=10,
        source_type="manual",
    )
    assert result2 is None

    result3 = await record_provider_call(
        tenant_id="00000000-0000-0000-0000-000000000001",
        operation="classification",
        status="succeeded",
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
