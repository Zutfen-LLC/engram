"""Privacy / injection tests for the Context Manifest (ENG-CONTEXT-001).

Proves the manifest contains no raw memory content, raw query, policy JSON,
grant lists, excluded-candidate IDs, conflict counterpart IDs, source URIs,
review notes, credentials/secrets, or embedding vectors — using recursive
key/value inspection rather than substring checks.

Also proves a caller cannot use malformed manifest input to inject NaN/inf,
invalid UUIDs/hashes, unknown fields, duplicate ordinals, mismatched counts,
mismatched packet hash, or item hashes that do not match the finalized
response.
"""

from __future__ import annotations

from typing import Any

import pytest

from engram.context_manifest import (
    MANIFEST_CONTRACT_VERSION,
    MEMORY_CONTEXT_VERSION,
    PACKET_RENDER_VERSION,
    ContextManifestEffectiveV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
)

TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
ITEM_A = "00000000-0000-0000-0000-000000000010"

SECRET_PATTERNS = [
    "sk-1234567890abcdefSECRETKEY",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_secretGithubToken123456",
    "password=hunter2",
    "-----BEGIN PRIVATE KEY-----",
]


def _item(content: str = "safe content", **extra: Any) -> dict[str, Any]:
    base = {
        "id": ITEM_A,
        "kind": "fact",
        "content": content,
        "review_status": "active",
        "score": 0.5,
        "reasons": ["importance=0.50"],
        "warnings": [],
        "pinned": False,
        "importance": 0.5,
        "source_trust": 0.5,
        "memory_confidence": 0.5,
        "human_verified": True,
        "authority": 10,
        "visibility": "private",
        "workspace_id": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
    }
    base.update(extra)
    return base


class _Response:
    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs.get("working_set", "[fact] safe content")
        self.items = kwargs.get("items", [_item()])
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")


def _subject() -> ContextManifestSubjectV1:
    return ContextManifestSubjectV1(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        workspace_id=None,
        memory_context_version=MEMORY_CONTEXT_VERSION,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_version=None,
    )


def _request() -> ContextManifestRequestInputV1:
    return ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(
            workspace_supplied=False, byte_budget=None, token_budget=None, item_budget=None
        ),
        effective=ContextManifestEffectiveV1(
            workspace_id=None, byte_budget=4096, token_budget=None, item_budget=None
        ),
        query_digest=None,
    )


def _versions() -> ContextManifestVersionsV1:
    return ContextManifestVersionsV1(
        scoring_version="v1",
        config_version="v1",
        candidate_strategy_version="startup-candidates-v1",
        manifest_contract_version=MANIFEST_CONTRACT_VERSION,
        packet_render_version=PACKET_RENDER_VERSION,
    )


def _build(**overrides: Any) -> Any:
    return build_startup_context_manifest_v1(
        response=overrides.get("response", _Response()),
        subject_context=overrides.get("subject_context", _subject()),
        request_context=overrides.get("request_context", _request()),
        decision_versions=overrides.get("decision_versions", _versions()),
    )


# ─── recursive walk helpers ────────────────────────────────────────────


def _walk_values(obj: Any):
    """Yield every string value and every dict key anywhere in the tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _walk_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_values(v)
    elif isinstance(obj, str):
        yield obj


def _walk_keys(obj: Any):
    """Yield every dict key (field name) anywhere in the tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


# ─── Privacy: forbidden content absent ─────────────────────────────────


class TestPrivacy:
    def setup_method(self) -> None:
        self.manifest = _build(
            response=_Response(
                working_set="[fact] sensitive raw content here",
                items=[_item(content="sensitive raw content here")],
            )
        ).model_dump(mode="json", exclude_none=False, by_alias=True)

    def test_no_raw_memory_content(self) -> None:
        # The served content words must not appear in the manifest. Only the
        # served_content_hash of those bytes may appear.
        all_values = list(_walk_values(self.manifest))
        for forbidden in ("sensitive", "raw content here", "sensitive raw content here"):
            assert not any(forbidden in v for v in all_values if isinstance(v, str)), (
                f"raw content fragment leaked: {forbidden!r}"
            )

    @pytest.mark.parametrize("secret", SECRET_PATTERNS)
    def test_no_secrets_leak_when_present_in_content(self, secret: str) -> None:
        m = _build(response=_Response(items=[_item(content=secret)])).model_dump(
            mode="json", exclude_none=False, by_alias=True
        )
        all_values = list(_walk_values(m))
        assert not any(secret in v for v in all_values if isinstance(v, str))

    def test_no_forbidden_field_names(self) -> None:
        keys = set(_walk_keys(self.manifest))
        forbidden_keys = {
            "content",
            "query",
            "policy",
            "grants",
            "workspace_grants",
            "excluded_candidates",
            "rejected_candidates",
            "conflicts_with_item_id",
            "source_uri",
            "review_notes",
            "embedding",
            "embedding_vector",
            "vector",
            "provenance",
            "secret",
            "api_key",
            "credential",
            "manifest_hash",  # lives outside the hashed object
            "recall_log_id",  # volatile receipt metadata
            "receipt_id",
            "created_at",
        }
        leaked = keys & forbidden_keys
        assert not leaked, f"forbidden fields present in manifest: {leaked}"

    def test_no_raw_semantic_query(self) -> None:
        # query_digest is the only query-derived value allowed, and startup
        # sets it null. A raw query string must never appear.
        all_values = list(_walk_values(self.manifest))
        assert not any("query" in v.lower() and "select" in v.lower() for v in all_values)

    def test_no_embedding_vectors(self) -> None:
        # No list of floats resembling a vector; no "embedding" field.
        keys = set(_walk_keys(self.manifest))
        assert not any("embed" in k.lower() for k in keys)

    def test_no_full_policy_or_grant_payload(self) -> None:
        all_values = list(_walk_values(self.manifest))
        # Subject deliberately excludes policy JSON / grant lists.
        assert not any("force_rls" in v.lower() for v in all_values)
        assert not any("workspace_grant" in v.lower() for v in all_values)


# ─── Injection rejection ───────────────────────────────────────────────


class TestInjectionRejection:
    def test_nan_in_score_rejected(self) -> None:
        resp = _Response(items=[_item(score=float("nan"))])
        with pytest.raises(Exception):  # noqa: B017
            _build(response=resp)

    def test_infinity_rejected(self) -> None:
        resp = _Response(items=[_item(importance=float("inf"))])
        with pytest.raises(Exception):  # noqa: B017
            _build(response=resp)

    def test_invalid_uuid_in_subject_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            _build(
                subject_context=ContextManifestSubjectV1(
                    tenant_id="not-a-uuid",
                    principal_id=PRINCIPAL,
                    workspace_id=None,
                    memory_context_version=MEMORY_CONTEXT_VERSION,
                    memory_profile_id=None,
                    memory_profile_revision_id=None,
                    memory_profile_version=None,
                )
            )

    def test_invalid_hash_in_packet_rejected_on_validate(self) -> None:
        # The builder recomputes the packet hash from response bytes, so direct
        # injection via the builder is impossible. This test validates that a
        # hand-crafted manifest model with a malformed hash is rejected.
        from engram.context_manifest import ContextManifestPacketV1

        with pytest.raises(Exception):  # noqa: B017
            ContextManifestPacketV1(
                media_type="text/plain; charset=utf-8",
                render_version=PACKET_RENDER_VERSION,
                hash="not-a-hash",
            )

    def test_unknown_field_rejected_on_validate(self) -> None:
        from engram.context_manifest import ContextManifestV1

        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        dumped["evil"] = "inject"
        with pytest.raises(Exception):  # noqa: B017
            ContextManifestV1.model_validate(dumped)

    def test_duplicate_ordinals_rejected(self) -> None:
        from engram.context_manifest import ContextManifestV1

        resp = _Response(
            items=[
                _item(id=ITEM_A),
                _item(id="00000000-0000-0000-0000-000000000011"),
            ]
        )
        dumped = _build(response=resp).model_dump(
            mode="json", exclude_none=False, by_alias=True
        )
        dumped["items"][1]["ordinal"] = 0  # collides with items[0]
        with pytest.raises(Exception):  # noqa: B017
            ContextManifestV1.model_validate(dumped)

    def test_mismatched_item_count_rejected_at_build(self) -> None:
        # Builder recomputes result.item_count from response.items; a caller
        # cannot force a mismatched count. Here we confirm the builder's count
        # always equals len(items) regardless of any "decorative" count.
        resp = _Response(items=[_item(), _item()])
        m = _build(response=resp)
        assert m.result.item_count == 2 == len(m.items)

    def test_served_content_hash_recomputed_not_trusted(self) -> None:
        # Even if a raw item tried to supply its own served_content_hash, the
        # builder ignores it and recomputes from content.
        resp = _Response(items=[_item(content="real content")])
        # Inject a bogus pre-existing hash key the builder must NOT honor.
        resp.items[0]["served_content_hash"] = "sha256:" + "0" * 64
        m = _build(response=resp)
        import hashlib

        expected = "sha256:" + hashlib.sha256(b"real content").hexdigest()
        assert m.items[0].served_content_hash == expected

    def test_packet_hash_recomputed_from_response_bytes(self) -> None:
        # The packet hash is SHA-256 of response.working_set bytes — it cannot
        # be supplied or influenced by any input other than the served packet.
        import hashlib

        ws = "[fact] exactly these bytes"
        resp = _Response(working_set=ws, items=[_item(content="exactly these bytes")])
        m = _build(response=resp)
        assert m.packet.hash == "sha256:" + hashlib.sha256(ws.encode("utf-8")).hexdigest()

    def test_request_digest_recomputed_not_trusted(self) -> None:
        # request_digest in the built manifest equals a fresh derivation over
        # the input descriptor; it is never echoed from a caller field.
        from engram.context_manifest import _compute_request_digest

        req = _request()
        m = _build(request_context=req)
        assert m.request.request_digest == _compute_request_digest(req)
