"""Network-free direct HTTPS transport and envelope contracts for #216."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from evals.calibration.api_reviewer_216 import (
    HTTPResponseCapture,
    HTTPSDirectTransport,
    ReviewerRoute216,
    extract_response_216,
    reviewer_routes_216,
)


def test_openrouter_extractor_rejects_ambiguous_choices_and_binds_version() -> None:
    route = reviewer_routes_216()["model_a"]
    raw = json.dumps(
        {
            "id": "req-1",
            "model": route.model,
            "choices": [
                {"message": {"content": "first"}},
                {"message": {"content": "second"}},
            ],
        }
    ).encode()
    with pytest.raises(ValueError, match="direct_api_response_choice_ambiguous"):
        extract_response_216(route, raw, {})


def test_http_capture_keeps_status_headers_and_exact_bytes() -> None:
    capture = HTTPResponseCapture(
        status=429,
        headers={"x-request-id": "safe-id", "content-type": "application/json"},
        body=b'{"error":"rate limited"}',
        final_url="https://openrouter.ai/api/v1/chat/completions",
    )
    assert capture.status == 429
    assert capture.body == b'{"error":"rate limited"}'
    assert capture.safe_headers == {"content-type": "application/json", "x-request-id": "safe-id"}
    assert capture.provider_request_id == "safe-id"


def test_extractor_requires_expected_model_and_rejects_tool_only_response() -> None:
    route: ReviewerRoute216 = reviewer_routes_216()["model_c"]
    raw = json.dumps(
        {
            "id": "zai-1",
            "model": route.model,
            "choices": [{"message": {"tool_calls": [{"id": "call"}]}}],
        }
    ).encode()
    with pytest.raises(ValueError, match="direct_api_response_content_invalid"):
        extract_response_216(route, raw, {})


def test_redirect_is_not_followed_and_authorization_cannot_reach_destination() -> None:
    """The production opener returns the original 302 and makes no Location request."""
    calls: list[tuple[str, str | None]] = []

    class RedirectingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/evil")
            self.send_header("x-request-id", "canonical")
            self.end_headers()
            self.wfile.write(b"redirect body\x00\xff")

        def log_message(self, format: str, *_args: object) -> None:
            del format
            return None

    server = HTTPServer(("127.0.0.1", 0), RedirectingHandler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/canonical"
        capture = HTTPSDirectTransport().post(
            url=url, headers={"Authorization": "Bearer never-forward"}, body=b"{}"
        )
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()

    assert calls == [("/canonical", "Bearer never-forward")]
    assert capture.status == 302
    assert capture.body == b"redirect body\x00\xff"
    assert capture.safe_headers == {"location": "/evil", "x-request-id": "canonical"}
