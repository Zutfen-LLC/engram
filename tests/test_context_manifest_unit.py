"""Unit tests for the canonical Context Manifest contract (ENG-CONTEXT-001).

These tests are pure (no database, no HTTP). They prove:
- deterministic byte-identical canonical manifests across constructions and
  processes;
- the builder has no database/session dependency and is immune to post-response
  mutation of mutable ORM/dict state;
- hash sensitivity (item reorder, score/reason/warning/review/trust/visibility/
  workspace/conflict/budget/profile changes alter the manifest hash; score and
  reason changes do NOT alter the packet hash; exact content changes alter all
  three hashes);
- rejection of NaN/±Infinity and unknown manifest fields;
- explicit-null serialization is unambiguous.

Route parity, privacy, and golden-vector coverage live in their own files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from engram.context_manifest import (
    MANIFEST_CONTRACT_VERSION,
    MEMORY_CONTEXT_VERSION,
    PACKET_RENDER_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    ContextManifestEffectiveV1,
    ContextManifestItemV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
    canonical_json_bytes,
    compute_manifest_hash,
    sha256_digest,
)

TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
WORKSPACE = "00000000-0000-0000-0000-000000000003"
ITEM_A = "00000000-0000-0000-0000-000000000010"
ITEM_B = "00000000-0000-0000-0000-000000000011"


def _item(
    *,
    id_: str = ITEM_A,
    kind: str = "fact",
    content: str = "hello",
    review_status: str = "active",
    score: float | None = 0.8123,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    pinned: bool = False,
    importance: float = 0.9,
    source_trust: float = 0.8,
    memory_confidence: float = 0.75,
    human_verified: bool = True,
    authority: int = 10,
    visibility: str = "private",
    workspace_id: str | None = None,
    conflict_type: str | None = None,
    conflict_resolution_status: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "content": content,
        "review_status": review_status,
        "score": score,
        "reasons": list(reasons) if reasons is not None else ["importance=0.90"],
        "warnings": list(warnings) if warnings is not None else [],
        "pinned": pinned,
        "importance": importance,
        "source_trust": source_trust,
        "memory_confidence": memory_confidence,
        "human_verified": human_verified,
        "authority": authority,
        "visibility": visibility,
        "workspace_id": workspace_id,
        "conflict_type": conflict_type,
        "conflict_resolution_status": conflict_resolution_status,
    }


class _Response:
    """Minimal finalized-response stand-in (satisfies RecallResponseLike)."""

    def __init__(
        self,
        *,
        working_set: str,
        items: list[dict[str, Any]],
        pinned_omitted_count: int = 0,
        omitted_count: int = 0,
        message: str | None = None,
    ) -> None:
        self.working_set = working_set
        self.items = items
        self.pinned_omitted_count = pinned_omitted_count
        self.omitted_count = omitted_count
        self.message = message


def _subject(
    *,
    workspace_id: str | None = None,
    profile: bool = False,
) -> ContextManifestSubjectV1:
    return ContextManifestSubjectV1(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        workspace_id=workspace_id,
        memory_context_version=MEMORY_CONTEXT_VERSION,
        memory_profile_id="00000000-0000-0000-0000-000000000090" if profile else None,
        memory_profile_revision_id=(
            "00000000-0000-0000-0000-000000000091" if profile else None
        ),
        memory_profile_version=3 if profile else None,
    )


def _request(
    *,
    workspace_supplied: bool = False,
    eff_byte: int | None = 4096,
    eff_workspace: str | None = None,
) -> ContextManifestRequestInputV1:
    return ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(
            workspace_supplied=workspace_supplied,
            byte_budget=None,
            token_budget=None,
            item_budget=None,
        ),
        effective=ContextManifestEffectiveV1(
            workspace_id=eff_workspace,
            byte_budget=eff_byte,
            token_budget=None,
            item_budget=None,
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


def _build_two_item_response() -> _Response:
    return _Response(
        working_set="[fact] alpha\n[preference] beta",
        items=[
            _item(id_=ITEM_A, content="alpha", score=0.81),
            _item(id_=ITEM_B, content="beta", score=0.55, kind="preference"),
        ],
        omitted_count=4,
    )


def _build(**overrides: Any) -> Any:
    return build_startup_context_manifest_v1(
        response=overrides.pop("response", _build_two_item_response()),
        subject_context=overrides.pop("subject_context", _subject()),
        request_context=overrides.pop("request_context", _request()),
        decision_versions=overrides.pop("decision_versions", _versions()),
    )


# ─── Determinism ───────────────────────────────────────────────────────


class TestDeterminism:
    def test_identical_inputs_produce_identical_manifest_hash(self) -> None:
        m1 = _build()
        m2 = _build()
        assert compute_manifest_hash(m1) == compute_manifest_hash(m2)
        # canonical bytes byte-identical too
        b1 = canonical_json_bytes(m1.model_dump(mode="json", exclude_none=False, by_alias=True))
        b2 = canonical_json_bytes(m2.model_dump(mode="json", exclude_none=False, by_alias=True))
        assert b1 == b2

    def test_repeated_construction_across_processes(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Serialize the exact response input to a file, then rebuild it in a
        # fresh subprocess. The manifest hash must match the in-process hash.
        m = _build()
        expected_hash = compute_manifest_hash(m)

        # Round-trip the manifest through JSON to simulate an independent
        # reconstruction of the *same canonical object* in another process.
        dumped = m.model_dump(mode="json", exclude_none=False, by_alias=True)
        script = tmp_path / "rebuild.py"
        script.write_text(
            "import json, sys\n"
            "from engram.context_manifest import canonical_json_bytes, sha256_digest\n"
            "obj = json.loads(sys.stdin.read())\n"
            "print(sha256_digest(canonical_json_bytes(obj)))\n"
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(dumped),
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert proc.stdout.strip() == expected_hash

    def test_key_insertion_order_does_not_change_canonical_bytes(self) -> None:
        # Two dicts with the same data but different key insertion order must
        # canonicalize to identical bytes (RFC 8785 orders by UTF-16 code unit).
        a = {"z": 1, "a": 2, "mid": 3, "Z": 4, "Aa": 5, "AA": 6}
        b = {"AA": 6, "Aa": 5, "z": 1, "Z": 4, "mid": 3, "a": 2}
        assert canonical_json_bytes(a) == canonical_json_bytes(b)


# ─── Response-only construction / mutation boundary ────────────────────


class TestResponseOnlyConstruction:
    def test_builder_has_no_database_dependency(self) -> None:
        # The builder accepts a plain finalized response object; no session,
        # ORM row, recall log, or repository is involved. Building from a
        # bare attribute object succeeds.
        m = _build()
        assert m.result.item_count == 2

    def test_mutating_response_items_after_build_does_not_change_manifest(self) -> None:
        response = _build_two_item_response()
        m = _build(response=response)
        before = compute_manifest_hash(m)
        before_packet = m.packet.hash

        # Mutate the underlying mutable dict / list after finalization.
        response.items[0]["score"] = 0.01
        response.items[0]["review_status"] = "disputed"
        response.items.append(_item(content="injected"))
        response.working_set = "[fact] tampered"

        # The already-built manifest is a frozen snapshot — unaffected.
        assert compute_manifest_hash(m) == before
        assert m.packet.hash == before_packet
        assert m.items[0].score == 0.81
        assert m.items[0].review_status == "active"
        assert m.result.item_count == 2

    def test_replacing_orm_fixture_afterward_leaves_manifest_intact(self) -> None:
        # The manifest snapshots served content via served_content_hash; it
        # never holds a live reference to ORM state.
        response = _build_two_item_response()
        m = _build(response=response)
        h0 = m.items[0].served_content_hash
        # SHA-256 of "alpha" UTF-8
        assert h0 == sha256_digest(b"alpha")
        # Even if the source content were later rewritten, the snapshot holds.
        response.items[0]["content"] = "totally different"
        assert m.items[0].served_content_hash == h0


# ─── Hash sensitivity ──────────────────────────────────────────────────


class TestHashSensitivity:
    def _hashes(self, **overrides: Any) -> tuple[str, str]:
        m = _build(**overrides)
        return compute_manifest_hash(m), m.packet.hash

    def test_item_reorder_changes_manifest_and_packet_hash(self) -> None:
        base_manifest, base_packet = self._hashes()
        swapped = _Response(
            working_set="[preference] beta\n[fact] alpha",
            items=[
                _item(id_=ITEM_B, content="beta", score=0.55, kind="preference"),
                _item(id_=ITEM_A, content="alpha", score=0.81),
            ],
            omitted_count=4,
        )
        m_hash, p_hash = self._hashes(response=swapped)
        assert m_hash != base_manifest
        assert p_hash != base_packet

    def test_score_change_alters_manifest_not_packet(self) -> None:
        base_manifest, base_packet = self._hashes()
        resp = _build_two_item_response()
        resp.items[0]["score"] = 0.99
        m_hash, p_hash = self._hashes(response=resp)
        assert m_hash != base_manifest
        assert p_hash == base_packet  # packet = rendered bytes; score not in it

    def test_reason_change_alters_manifest_not_packet(self) -> None:
        base_manifest, base_packet = self._hashes()
        resp = _build_two_item_response()
        resp.items[0]["reasons"] = ["importance=0.10"]
        m_hash, p_hash = self._hashes(response=resp)
        assert m_hash != base_manifest
        assert p_hash == base_packet

    def test_warning_change_alters_manifest_not_packet(self) -> None:
        base_manifest, base_packet = self._hashes()
        resp = _build_two_item_response()
        resp.items[0]["warnings"] = ["low confidence"]
        m_hash, p_hash = self._hashes(response=resp)
        assert m_hash != base_manifest
        assert p_hash == base_packet

    @pytest.mark.parametrize(
        "field,value",
        [
            ("review_status", "disputed"),
            ("authority", 99),
            ("visibility", "tenant"),
            ("workspace_id", WORKSPACE),
            ("conflict_type", "contradiction"),
            ("conflict_resolution_status", "superseded"),
            ("human_verified", False),
            ("importance", 0.12),
            ("source_trust", 0.30),
            ("memory_confidence", 0.40),
            ("pinned", True),
        ],
    )
    def test_decision_field_change_alters_manifest_hash(
        self, field: str, value: Any
    ) -> None:
        base_manifest, _ = self._hashes()
        resp = _build_two_item_response()
        resp.items[0][field] = value
        m_hash, _ = self._hashes(response=resp)
        assert m_hash != base_manifest, f"changing {field} should alter manifest hash"

    def test_exact_content_change_alters_all_three_hashes(self) -> None:
        base_manifest, base_packet = self._hashes()
        base_item_hash = _build().items[0].served_content_hash

        resp = _build_two_item_response()
        # Whitespace-only content change (the dedup hash would NOT catch this).
        resp.items[0]["content"] = "alpha "  # trailing space
        resp.working_set = "[fact] alpha \n[preference] beta"
        m = _build(response=resp)

        assert compute_manifest_hash(m) != base_manifest
        assert m.packet.hash != base_packet
        assert m.items[0].served_content_hash != base_item_hash

    def test_case_only_content_change_alters_served_content_hash(self) -> None:
        # Proves served_content_hash does NOT reuse engram.canonicalize (which
        # lowercases + collapses whitespace).
        lower = sha256_digest(b"Hello World")
        upper = sha256_digest(b"hello world")
        assert lower != upper
        # And the manifest builder uses the exact-bytes hash:
        resp = _Response(
            working_set="[fact] Hello World",
            items=[_item(content="Hello World")],
        )
        assert _build(response=resp).items[0].served_content_hash == lower

    def test_requested_effective_budget_change_alters_manifest_hash(self) -> None:
        base_manifest, _ = self._hashes()
        m_hash, _ = self._hashes(request_context=_request(eff_byte=8192))
        assert m_hash != base_manifest

    def test_profile_revision_change_alters_manifest_hash(self) -> None:
        no_profile, _ = self._hashes()
        with_profile, _ = self._hashes(subject_context=_subject(profile=True))
        assert with_profile != no_profile

    def test_workspace_change_alters_manifest_hash(self) -> None:
        base_manifest, _ = self._hashes()
        ws_manifest, _ = self._hashes(
            subject_context=_subject(workspace_id=WORKSPACE),
            request_context=_request(eff_workspace=WORKSPACE),
        )
        assert ws_manifest != base_manifest


# ─── Packet / count semantics ──────────────────────────────────────────


class TestPacketSemantics:
    def test_empty_packet_hashes_as_sha256_of_zero_bytes(self) -> None:
        resp = _Response(working_set="", items=[])
        m = _build(response=resp)
        empty_hash = sha256_digest(b"")
        assert m.packet.hash == empty_hash
        assert m.result.item_count == 0
        assert m.result.rendered_packet_byte_count == 0
        assert m.result.served_content_byte_count == 0

    def test_served_vs_rendered_byte_counts_differ(self) -> None:
        # working_set = "[fact] alpha\n[preference] beta" (30 bytes)
        # served content = "alpha" + "beta" = 9 bytes
        m = _build()
        assert m.result.served_content_byte_count == 9
        assert m.result.rendered_packet_byte_count == len(
            b"[fact] alpha\n[preference] beta"
        )
        assert m.result.served_content_byte_count != m.result.rendered_packet_byte_count

    def test_packet_hash_matches_response_working_set_bytes(self) -> None:
        resp = _build_two_item_response()
        m = _build(response=resp)
        assert m.packet.hash == sha256_digest(resp.working_set.encode("utf-8"))

    def test_ordinals_match_array_position(self) -> None:
        m = _build()
        assert [i.ordinal for i in m.items] == [0, 1]


# ─── Rejection rules ───────────────────────────────────────────────────


class TestRejection:
    def test_nan_score_rejected(self) -> None:
        resp = _build_two_item_response()
        resp.items[0]["score"] = float("nan")
        with pytest.raises(Exception):  # noqa: B017 - pydantic/validation error
            _build(response=resp)

    def test_infinity_importance_rejected(self) -> None:
        resp = _build_two_item_response()
        resp.items[0]["importance"] = float("inf")
        with pytest.raises(Exception):  # noqa: B017
            _build(response=resp)

    def test_negative_zero_canonicalized_to_zero(self) -> None:
        # RFC 8785 §3.2.2.3 collapses -0 to 0. The manifest accepts -0.0 input
        # (it is finite) but canonicalizes it to 0 in the hashed bytes.
        resp = _Response(
            working_set="[fact] z",
            items=[_item(content="z", score=-0.0, importance=-0.0)],
        )
        m = _build(response=resp)
        canon = canonical_json_bytes(
            m.model_dump(mode="json", exclude_none=False, by_alias=True)
        )
        assert b'"score":0' in canon or b'"score":0.0' in canon or b'-0' not in canon

    def test_unknown_top_level_field_rejected(self) -> None:
        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        dumped["unexpected_field"] = "no"
        with pytest.raises(Exception):  # noqa: B017
            from engram.context_manifest import ContextManifestV1

            ContextManifestV1.model_validate(dumped)

    def test_unknown_item_field_rejected(self) -> None:
        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        dumped["items"][0]["content"] = "leak"  # content is NOT a manifest field
        with pytest.raises(Exception):  # noqa: B017
            from engram.context_manifest import ContextManifestV1

            ContextManifestV1.model_validate(dumped)

    def test_invalid_uuid_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ContextManifestSubjectV1(
                tenant_id="not-a-uuid",
                principal_id=PRINCIPAL,
                workspace_id=None,
                memory_context_version=MEMORY_CONTEXT_VERSION,
                memory_profile_id=None,
                memory_profile_revision_id=None,
                memory_profile_version=None,
            )

    def test_invalid_hash_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ContextManifestItemV1(
                ordinal=0,
                item_id=ITEM_A,
                kind="fact",
                served_content_hash="not-a-hash",
                review_status="active",
                authority=10,
                visibility="private",
                workspace_id=None,
                score=0.5,
                importance=0.5,
                source_trust=0.5,
                memory_confidence=0.5,
                reasons=[],
                warnings=[],
                pinned=False,
                human_verified=True,
                conflict_type=None,
                conflict_resolution_status=None,
            )

    def test_half_profile_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ContextManifestSubjectV1(
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                workspace_id=None,
                memory_context_version=MEMORY_CONTEXT_VERSION,
                memory_profile_id="00000000-0000-0000-0000-000000000090",
                memory_profile_revision_id=None,  # half-specified
                memory_profile_version=3,
            )

    def test_duplicate_or_out_of_order_ordinals_rejected(self) -> None:
        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        dumped["items"][0]["ordinal"] = 1  # collides with items[1]
        with pytest.raises(Exception):  # noqa: B017
            from engram.context_manifest import ContextManifestV1

            ContextManifestV1.model_validate(dumped)

    def test_manifest_hash_not_inside_hashed_object(self) -> None:
        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        assert "manifest_hash" not in dumped

    def test_unsupported_mode_rejected(self) -> None:
        dumped = _build().model_dump(mode="json", exclude_none=False, by_alias=True)
        dumped["mode"] = "semantic"
        with pytest.raises(Exception):  # noqa: B017
            from engram.context_manifest import ContextManifestV1

            ContextManifestV1.model_validate(dumped)


# ─── Explicit-null / omitted equivalence ───────────────────────────────


class TestExplicitNull:
    def test_optional_null_fields_serialize_explicitly(self) -> None:
        m = _build()
        dumped = m.model_dump(mode="json", exclude_none=False, by_alias=True)
        # workspace_id, query_digest, score (pinned), message, conflict_type,
        # conflict_resolution_status, memory_profile_* are all explicitly null
        # in the canonical bytes — no ambiguity between omitted and null.
        assert dumped["subject"]["workspace_id"] is None
        assert dumped["subject"]["memory_profile_id"] is None
        assert dumped["request"]["query_digest"] is None
        assert dumped["result"]["message"] is None
        canon = canonical_json_bytes(dumped)
        assert b'null' in canon


# ─── Protocol markers ──────────────────────────────────────────────────


class TestProtocolMarkers:
    def test_top_level_markers(self) -> None:
        m = _build()
        dumped = m.model_dump(mode="json", exclude_none=False, by_alias=True)
        assert dumped["schema"] == SCHEMA
        assert dumped["schema_version"] == SCHEMA_VERSION
        assert dumped["canonicalization"] == "rfc8785"
        assert dumped["mode"] == "startup"

    def test_versions_carry_runtime_values(self) -> None:
        m = _build()
        assert m.versions.manifest_contract_version == MANIFEST_CONTRACT_VERSION
        assert m.versions.packet_render_version == PACKET_RENDER_VERSION
        assert m.packet.render_version == PACKET_RENDER_VERSION

    def test_request_digest_excludes_itself(self) -> None:
        # request_digest is SHA-256 of the request descriptor WITHOUT the
        # request_digest field. The manifest's stored digest must equal a
        # fresh derivation from the input descriptor.
        req = _request()
        m = _build(request_context=req)
        from engram.context_manifest import _compute_request_digest

        assert m.request.request_digest == _compute_request_digest(req)

    def test_request_digest_stable_under_field_reorder(self) -> None:
        from engram.context_manifest import _compute_request_digest

        req = _request()
        d1 = _compute_request_digest(req)
        # Rebuild the input with reordered nested keys — same digest (RFC 8785
        # orders members by UTF-16 code unit, not insertion order).
        req2 = ContextManifestRequestInputV1(
            effective=req.effective,
            requested=req.requested,
            query_digest=req.query_digest,
        )
        assert _compute_request_digest(req2) == d1
