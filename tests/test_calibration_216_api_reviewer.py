"""Contract-first tests for #216's direct HTTP API reviewer lanes.

The implementation intentionally does not exist yet.  These tests define a
small injected-transport boundary: provider credentials are used only to build
request headers, while request/response bytes and extracted model content are
retained as digest-bound evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

import evals.calibration.api_reviewer_216 as api_reviewer_216
from evals.calibration.api_reviewer_216 import (
    APIReviewer216,
    CredentialUnavailableError,
    DirectAPIReviewAttempt216,
    HTTPResponseCapture,
    Transport,
    reviewer_routes_216,
    verify_direct_api_attempt_216,
)


class FakeTransport:
    """A deterministic, network-free transport recording exact HTTP inputs."""

    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    def post(self, *, url: str, headers: dict[str, str], body: bytes) -> bytes:
        self.calls.append((url, headers, body))
        return self.responses.pop(0)


_SAMPLE = {
    "sample_id": "s0123456789abcdef01234567",
    "case": {"sample_id": "s0123456789abcdef01234567", "content": "private source text"},
    "labeling_instructions": "Return one required JSON review object.",
}


def _judged_content() -> str:
    return (
        '{"sample_id":"s0123456789abcdef01234567","outcome":"judged",'
        '"judgment":{"fields":{"expected_kind":"fact","retention_value":"retain",'
        '"epistemic_state":"adequately_supported","consequence":"low",'
        '"acceptable_abstention":"no"},"reviewer_confidence":"medium"}}'
    )


def _chat_response(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _reviewer(transport: Transport, **credentials: str) -> APIReviewer216:
    patcher = pytest.MonkeyPatch()
    patcher.setattr(api_reviewer_216, "_CONCRETE_TRANSPORT_TYPE", lambda: transport)
    try:
        return APIReviewer216(credentials=credentials)
    finally:
        patcher.undo()


def test_routes_freeze_exact_openrouter_models_and_no_fallback_provider_choices() -> None:
    routes = reviewer_routes_216()

    sonnet = routes["model_a"]
    terra = routes["model_b"]
    assert sonnet.endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert sonnet.model == "anthropic/claude-sonnet-5"
    assert sonnet.provider_preferences == {
        "only": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert terra.endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert terra.model == "openai/gpt-5.6-terra"
    assert terra.provider_preferences == {
        "only": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }

    for route in (sonnet, terra):
        body = route.request_body(_SAMPLE)
        assert body["model"] == route.model
        assert body["provider"] == route.provider_preferences
        assert set(body) == {"model", "messages", "provider"}


def test_zai_route_uses_its_direct_endpoint_and_exact_glm_model() -> None:
    route = reviewer_routes_216()["model_c"]
    assert route.endpoint == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert route.model == "glm-5.3"
    assert route.provider_preferences is None
    assert route.request_body(_SAMPLE)["model"] == "glm-5.3"


def test_configuration_and_serialized_request_never_contain_credentials() -> None:
    secret = "super-secret-api-token"
    transport = FakeTransport([_chat_response(_judged_content())])
    reviewer = _reviewer(transport, openrouter=secret, zai=secret)

    serialized_config = reviewer.serialized_configuration()
    accepted = reviewer.review("model_a", _SAMPLE)
    _url, headers, body = transport.calls[0]

    assert secret not in serialized_config.decode()
    assert secret not in body.decode()
    assert headers["Authorization"] == f"Bearer {secret}"
    assert secret not in accepted.model_dump_json()


def test_missing_credential_fails_before_transport_is_called() -> None:
    transport = FakeTransport([])
    reviewer = _reviewer(transport)

    with pytest.raises(CredentialUnavailableError, match="direct_api_reviewer_credential_missing"):
        reviewer.review("model_a", _SAMPLE)

    assert transport.calls == []


def test_one_sample_request_has_a_single_direct_http_call_and_extracts_content() -> None:
    raw_response = _chat_response(_judged_content())
    transport = FakeTransport([raw_response])
    attempt = _reviewer(transport, openrouter="token").review("model_a", _SAMPLE)

    assert len(transport.calls) == 1
    url, _headers, raw_request = transport.calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert attempt.sample_id == _SAMPLE["sample_id"]
    assert attempt.raw_request == raw_request
    assert attempt.raw_response == raw_response
    assert attempt.extracted_content == _judged_content()
    assert attempt.parse_status == "parsed"
    assert attempt.outcome_status == "judged"
    verify_direct_api_attempt_216(attempt)


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    [
        ("raw_request", b'{"model":"substituted"}', "direct_api_request_digest_mismatch"),
        ("raw_response", b'{"choices":[]}', "direct_api_response_digest_mismatch"),
        (
            "extracted_content",
            "substituted content",
            "direct_api_extracted_content_digest_mismatch",
        ),
    ],
)
def test_raw_request_response_and_extracted_content_mutations_fail_closed(
    field: str, replacement: bytes | str, error: str
) -> None:
    transport = FakeTransport([_chat_response(_judged_content())])
    attempt = _reviewer(transport, openrouter="token").review("model_a", _SAMPLE)
    assert isinstance(attempt, DirectAPIReviewAttempt216)

    with pytest.raises(ValueError, match=error):
        verify_direct_api_attempt_216(replace(attempt, **{field: replacement}))


def test_http_non_2xx_is_retained_as_a_first_class_attempt() -> None:
    raw_response = b'{"error":{"message":"busy"}}'

    class HTTPFailureTransport:
        def post(self, *, url: str, headers: dict[str, str], body: bytes) -> HTTPResponseCapture:
            del headers, body
            return HTTPResponseCapture(503, {"x-request-id": "retry-id"}, raw_response, url)

    attempt = _reviewer(HTTPFailureTransport(), OPENROUTER_API_KEY="token").review(
        "model_a", _SAMPLE
    )

    assert attempt.http_status == 503
    assert attempt.raw_response == raw_response
    assert attempt.response_safe_headers == {"x-request-id": "retry-id"}
    assert attempt.outcome_status == "provider_error"
    assert attempt.failure_class == "retryable_http"
    assert attempt.retryable is True


def test_redirect_capture_is_terminal_authority_failure() -> None:
    class RedirectTransport:
        def post(self, *, url: str, headers: dict[str, str], body: bytes) -> HTTPResponseCapture:
            del headers, body
            return HTTPResponseCapture(302, {"location": "https://evil.invalid/"}, b"redirect", url)

    attempt = _reviewer(RedirectTransport(), OPENROUTER_API_KEY="token").review("model_a", _SAMPLE)

    assert attempt.http_status == 302
    assert attempt.failure_class == "authority_route_redirect"
    assert attempt.retryable is False
    assert attempt.outcome_status == "provider_error"


def test_refusal_is_preserved_as_response_bytes_without_a_retry() -> None:
    refusal = '{"sample_id":"s0123456789abcdef01234567","outcome":"refused","error_code":"scope"}'
    transport = FakeTransport([_chat_response(refusal), _chat_response(_judged_content())])
    attempt = _reviewer(transport, openrouter="token").review("model_a", _SAMPLE)

    assert attempt.parse_status == "malformed"
    assert attempt.outcome_status == "refused"
    assert attempt.error_code == "scope"
    assert attempt.raw_response == _chat_response(refusal)
    assert len(transport.calls) == 1


def test_malformed_response_is_preserved_with_no_extracted_judgment() -> None:
    raw_response = b'{"choices":[{"message":{"content":"not valid reviewer json"}}]}'
    transport = FakeTransport([raw_response])
    attempt = _reviewer(transport, zai="token").review("model_c", _SAMPLE)

    assert attempt.parse_status == "malformed"
    assert attempt.outcome_status == "malformed"
    assert attempt.extracted_content == "not valid reviewer json"
    assert attempt.judgment is None
    verify_direct_api_attempt_216(attempt)


@pytest.mark.parametrize(
    ("raw_response", "expected_failure_class", "expected_error", "expected_reported_model"),
    [
        (
            json.dumps(
                {
                    "model": "openai/gpt-5.6-terra",
                    "choices": [{"message": {"content": _judged_content()}}],
                }
            ).encode(),
            "authority_model_mismatch",
            "direct_api_response_model_mismatch",
            "openai/gpt-5.6-terra",
        ),
        (
            _chat_response(
                _judged_content().replace(_SAMPLE["sample_id"], "s99999999999999999999999")
            ),
            "authority_sample_mismatch",
            "response_sample_id_mismatch",
            None,
        ),
    ],
)
def test_2xx_identity_mismatches_are_durable_nonretryable_authority_failures(
    raw_response: bytes,
    expected_failure_class: str,
    expected_error: str,
    expected_reported_model: str | None,
) -> None:
    transport = FakeTransport([raw_response])

    attempt = _reviewer(transport, openrouter="token").review("model_a", _SAMPLE)

    assert len(transport.calls) == 1
    assert attempt.raw_response == raw_response
    assert attempt.http_status == 200
    assert attempt.outcome_status == "provider_error"
    assert attempt.parse_status == "absent"
    assert attempt.failure_class == expected_failure_class
    assert attempt.error_code == expected_error
    assert attempt.reported_model == expected_reported_model
    assert attempt.extracted_content
    assert attempt.judgment is None
    assert attempt.retryable is False
    verify_direct_api_attempt_216(attempt)


def test_accepted_response_never_retries_even_when_more_attempts_are_allowed() -> None:
    transport = FakeTransport(
        [_chat_response(_judged_content()), _chat_response(_judged_content())]
    )
    attempt = _reviewer(transport, openrouter="token").review("model_b", _SAMPLE)

    assert attempt.outcome_status == "judged"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("raw", [b"ordinary utf-8\x00", b"\xff\xfe\x00binary-error"])
def test_raw_byte_envelopes_round_trip_losslessly(raw: bytes) -> None:
    attempt = _reviewer(
        FakeTransport([_chat_response(_judged_content())]), openrouter="token"
    ).review("model_a", _SAMPLE)
    serialized = replace(
        attempt, raw_response=raw, raw_response_sha256=hashlib.sha256(raw).hexdigest()
    ).model_dump()

    restored = DirectAPIReviewAttempt216.from_dump(serialized)

    assert restored.raw_response == raw
    envelope = serialized["raw_response"]
    assert envelope["sha256"] == hashlib.sha256(raw).hexdigest()
    assert envelope["encoding"] == (
        "utf-8" if raw.decode("utf-8", errors="ignore").encode() == raw else "base64"
    )


def test_byte_envelope_tampering_fails_closed() -> None:
    raw = b"\xff\x00binary"
    attempt = _reviewer(
        FakeTransport([_chat_response(_judged_content())]), openrouter="token"
    ).review("model_a", _SAMPLE)
    serialized = replace(
        attempt, raw_response=raw, raw_response_sha256=hashlib.sha256(raw).hexdigest()
    ).model_dump()
    envelope = serialized["raw_response"]
    assert isinstance(envelope, dict)

    for mutation in (
        {**envelope, "data": envelope["data"] + "A"},
        {**envelope, "encoding": "utf-8"},
        {**envelope, "sha256": "0" * 64},
    ):
        tampered = {**serialized, "raw_response": mutation}
        with pytest.raises(ValueError, match="direct_api_attempt_serialized_bytes"):
            DirectAPIReviewAttempt216.from_dump(tampered)
