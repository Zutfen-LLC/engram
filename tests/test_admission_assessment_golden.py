"""Golden vectors for the ``engram.admission-assessment.v1`` decision hash.

These are frozen bytes and frozen digests, not recomputations. If a change to
canonicalization, field ordering, key naming, or the envelope's field set
alters the hash, these fail — which is the point: ``decision_hash`` is a
durable identity that a later Context Ledger binding (#160) and any external
verifier must be able to resolve after arbitrary later policy and item
changes.

Cross-runtime agreement is proven structurally rather than by shelling out to
a JS runtime: the vectors pin the exact RFC 8785 canonical byte sequence, so
any implementation that produces those bytes produces the same SHA-256. The
independent recomputation below re-derives each digest from the pinned bytes
without going through the library that produced them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engram.admission_assessment import (
    POLICY_CONTRACT_VERSION,
    POLICY_PROFILE_KEY,
    SCHEMA_VERSION,
    canonical_bytes,
    decision_hash,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "admission-assessment-v1.schema.json"
)

_TENANT = "11111111-1111-4111-8111-111111111111"
_ITEM = "22222222-2222-4222-8222-222222222222"
_CONTENT_HASH = "sha256:" + "a" * 64
_INPUT_DIGEST = "sha256:" + "b" * 64
_POLICY_DIGEST = "sha256:" + "c" * 64


def _envelope(**overrides: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "tenant_id": _TENANT,
        "memory_item_id": _ITEM,
        "mode": "authoritative",
        "item_content_hash": _CONTENT_HASH,
        "input_digest": _INPUT_DIGEST,
        "policy_profile_key": POLICY_PROFILE_KEY,
        "policy_contract_version": POLICY_CONTRACT_VERSION,
        "policy_config_digest": _POLICY_DIGEST,
        "selected_basis": None,
        "outcome": "cooling",
        "blocker_codes": ["age"],
        "reason_codes": ["lane_qualified_awaiting_age", "no_lane_qualified"],
        "decision_inputs": {
            "legacy_age_qualified": False,
            "legacy_trust_qualified": True,
            "memory_confidence": 0.9,
            "min_age_hours": 72,
        },
        "conflict_recheck_status": "not_run",
        "cooling_period_start": "2026-02-01T00:00:00+00:00",
        "eligible_at": "2026-02-04T00:00:00+00:00",
        "next_evaluation_at": "2026-02-04T00:00:00+00:00",
        "next_actions": ["wait_until"],
    }
    envelope.update(overrides)
    return envelope


# Frozen (envelope, canonical bytes, digest) triples. Changing any of these
# without a schema version bump is a breaking change to an externally
# verifiable contract.
GOLDEN: list[tuple[str, dict[str, object], bytes, str]] = [
    (
        "cooling",
        _envelope(),
        b'{"blocker_codes":["age"],"conflict_recheck_status":"not_run","cooling_period_start":"2026-02-01T00:00:00+00:00","decision_inputs":{"legacy_age_qualified":false,"legacy_trust_qualified":true,"memory_confidence":0.9,"min_age_hours":72},"eligible_at":"2026-02-04T00:00:00+00:00","input_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","item_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","memory_item_id":"22222222-2222-4222-8222-222222222222","mode":"authoritative","next_actions":["wait_until"],"next_evaluation_at":"2026-02-04T00:00:00+00:00","outcome":"cooling","policy_config_digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","policy_contract_version":"path-a-compat-v1","policy_profile_key":"path_a_compat","reason_codes":["lane_qualified_awaiting_age","no_lane_qualified"],"schema_version":"engram.admission-assessment.v1","selected_basis":null,"tenant_id":"11111111-1111-4111-8111-111111111111"}',
        "sha256:807ff899f413245bc2b7eeb07c1c6faff4068d2bb47d5eec3293fbf6b7ebb625",
    ),
    (
        "admitted",
        _envelope(
            outcome="admitted",
            selected_basis="legacy_confidence",
            blocker_codes=[],
            reason_codes=["lane_selected_legacy_confidence", "mutation_committed"],
            conflict_recheck_status="clear",
            next_actions=["none"],
            next_evaluation_at=None,
        ),
        b'{"blocker_codes":[],"conflict_recheck_status":"clear","cooling_period_start":"2026-02-01T00:00:00+00:00","decision_inputs":{"legacy_age_qualified":false,"legacy_trust_qualified":true,"memory_confidence":0.9,"min_age_hours":72},"eligible_at":"2026-02-04T00:00:00+00:00","input_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","item_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","memory_item_id":"22222222-2222-4222-8222-222222222222","mode":"authoritative","next_actions":["none"],"next_evaluation_at":null,"outcome":"admitted","policy_config_digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","policy_contract_version":"path-a-compat-v1","policy_profile_key":"path_a_compat","reason_codes":["lane_selected_legacy_confidence","mutation_committed"],"schema_version":"engram.admission-assessment.v1","selected_basis":"legacy_confidence","tenant_id":"11111111-1111-4111-8111-111111111111"}',
        "sha256:0fa564f4772d9ba78808425d8ad1ab2da058998dc9768e5b72bd499d546bfa0d",
    ),
    (
        "blocked_shadow",
        _envelope(
            mode="shadow",
            outcome="blocked",
            selected_basis="retention_evidence",
            blocker_codes=["conflict_recheck"],
            reason_codes=[
                "conflict_recheck_blocked",
                "lane_selected_retention_evidence",
            ],
            conflict_recheck_status="not_run_preview",
            next_actions=["conflict_resolution_required"],
            next_evaluation_at=None,
        ),
        b'{"blocker_codes":["conflict_recheck"],"conflict_recheck_status":"not_run_preview","cooling_period_start":"2026-02-01T00:00:00+00:00","decision_inputs":{"legacy_age_qualified":false,"legacy_trust_qualified":true,"memory_confidence":0.9,"min_age_hours":72},"eligible_at":"2026-02-04T00:00:00+00:00","input_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","item_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","memory_item_id":"22222222-2222-4222-8222-222222222222","mode":"shadow","next_actions":["conflict_resolution_required"],"next_evaluation_at":null,"outcome":"blocked","policy_config_digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","policy_contract_version":"path-a-compat-v1","policy_profile_key":"path_a_compat","reason_codes":["conflict_recheck_blocked","lane_selected_retention_evidence"],"schema_version":"engram.admission-assessment.v1","selected_basis":"retention_evidence","tenant_id":"11111111-1111-4111-8111-111111111111"}',
        "sha256:1ded69af709738f558b8ac787a9fe97b9e71db3cbc075132ca4c15b86eca411b",
    ),
]


@pytest.mark.parametrize("name", [vector[0] for vector in GOLDEN])
def test_canonical_bytes_match_the_frozen_vector(name: str) -> None:
    """RFC 8785 output is byte-exact: keys sorted by Unicode code point, no
    insignificant whitespace, no BOM."""
    _, envelope, expected_bytes, _ = next(v for v in GOLDEN if v[0] == name)
    assert canonical_bytes(envelope) == expected_bytes


@pytest.mark.parametrize("name", [vector[0] for vector in GOLDEN])
def test_decision_hash_matches_the_frozen_vector(name: str) -> None:
    _, envelope, _, expected_hash = next(v for v in GOLDEN if v[0] == name)
    assert decision_hash(envelope) == expected_hash


@pytest.mark.parametrize("name", [vector[0] for vector in GOLDEN])
def test_any_runtime_producing_these_bytes_produces_this_digest(name: str) -> None:
    """The cross-runtime guarantee, restated without the canonicalizer: a
    verifier in another language only has to agree on RFC 8785 bytes; the
    SHA-256 over them is then forced."""
    _, _, canonical, expected_hash = next(v for v in GOLDEN if v[0] == name)
    assert "sha256:" + hashlib.sha256(canonical).hexdigest() == expected_hash


def test_hash_is_stable_under_python_dict_insertion_order() -> None:
    """A verifier must not depend on the order a producer happened to build
    its object in."""
    envelope = _envelope()
    shuffled = dict(reversed(list(envelope.items())))
    assert list(shuffled) != list(envelope)
    assert decision_hash(shuffled) == decision_hash(envelope)


def test_hash_changes_when_any_hashed_field_changes() -> None:
    base = decision_hash(_envelope())
    for field, value in (
        ("mode", "shadow"),
        ("outcome", "insufficient_evidence"),
        ("selected_basis", "legacy_confidence"),
        ("blocker_codes", ["age", "confidence"]),
        ("reason_codes", ["no_lane_qualified"]),
        ("conflict_recheck_status", "not_run_preview"),
        ("next_actions", ["none"]),
        ("next_evaluation_at", None),
        ("policy_config_digest", "sha256:" + "d" * 64),
        ("input_digest", "sha256:" + "d" * 64),
        ("decision_inputs", {"memory_confidence": 0.91}),
    ):
        assert decision_hash(_envelope(**{field: value})) != base, field


def test_golden_envelopes_validate_against_the_published_schema() -> None:
    """The schema shipped for external verifiers describes the bytes actually
    hashed, not an aspirational shape."""
    schema = json.loads(SCHEMA_PATH.read_text())
    required = set(schema["required"])
    allowed = set(schema["properties"])
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    for name, envelope, _, _ in GOLDEN:
        assert set(envelope) == required == allowed, name
        assert envelope["outcome"] in schema["properties"]["outcome"]["enum"], name
        for code in envelope["blocker_codes"]:  # type: ignore[union-attr]
            assert code in schema["properties"]["blocker_codes"]["items"]["enum"], name
        for code in envelope["reason_codes"]:  # type: ignore[union-attr]
            assert code in schema["properties"]["reason_codes"]["items"]["enum"], name
        for action in envelope["next_actions"]:  # type: ignore[union-attr]
            assert action in schema["properties"]["next_actions"]["items"]["enum"], name


def test_schema_enums_match_the_module_vocabularies() -> None:
    """The published schema and the runtime cannot drift: a new outcome or
    next action must land in both."""
    from engram.admission_assessment import (
        ADMISSION_OUTCOMES,
        ADMISSION_REASON_CODES,
        CONFLICT_RECHECK_STATUSES,
        NEXT_ACTION_ORDER,
    )
    from engram.promotion import PROMOTION_BLOCKER_CODES

    schema = json.loads(SCHEMA_PATH.read_text())
    props = schema["properties"]
    assert set(props["outcome"]["enum"]) == ADMISSION_OUTCOMES
    assert set(props["next_actions"]["items"]["enum"]) == set(NEXT_ACTION_ORDER)
    assert set(props["reason_codes"]["items"]["enum"]) == ADMISSION_REASON_CODES
    assert set(props["blocker_codes"]["items"]["enum"]) == PROMOTION_BLOCKER_CODES
    assert set(props["conflict_recheck_status"]["enum"]) == CONFLICT_RECHECK_STATUSES
