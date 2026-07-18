#!/usr/bin/env python3
"""Generate the ENG-CONTEXT-001 shared negative conformance fixtures.

The negative fixtures are **language-neutral rejection proofs**. Each fixture
carries a full valid vector ``input`` with exactly one field mutated to violate
the v1 contract. Both the Python verifier
(``scripts/verify_context_manifest_negatives.py``) and the JavaScript verifier
(``conformance/context-manifest-v1/verify_negatives.mjs``) must reject each
fixture; agreement between the two is the cross-language rejection proof.

A negative fixture intentionally carries NO expected valid hashes — it must be
rejected before any manifest is constructed.

Usage::

    python scripts/generate_context_manifest_negatives.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Allow running from repo root without an installed package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NEGATIVES_DIR = ROOT / "conformance" / "context-manifest-v1" / "negative"

# A single valid base input (derived from vector 002). Each mutation deep-copies
# this base and changes exactly one field, so each fixture isolates one rule.
TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
ITEM_1 = "00000000-0000-0000-0000-0000000000a1"
ITEM_2 = "00000000-0000-0000-0000-0000000000a2"
PROFILE = "00000000-0000-0000-0000-00000000009a"
PROFILE_REV = "00000000-0000-0000-0000-00000000009b"


def _base_input() -> dict[str, Any]:
    """A valid, coherent base input (mirrors vector 002's shape)."""
    return {
        "response": {
            "working_set": "[fact] alpha\n[preference] beta",
            "item_count": 2,
            "byte_count": 9,
            "pinned_omitted_count": 0,
            "omitted_count": 4,
            "message": None,
            "items": [
                {
                    "id": ITEM_1,
                    "kind": "fact",
                    "content": "alpha",
                    "review_status": "active",
                    "score": None,
                    "reasons": [],
                    "warnings": [],
                    "pinned": True,
                    "importance": 0.9,
                    "source_trust": 0.8,
                    "memory_confidence": 0.75,
                    "human_verified": True,
                    "authority": 10,
                    "visibility": "private",
                    "workspace_id": None,
                    "conflict_type": None,
                    "conflict_resolution_status": None,
                },
                {
                    "id": ITEM_2,
                    "kind": "preference",
                    "content": "beta",
                    "review_status": "active",
                    "score": 0.55,
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
                },
            ],
        },
        "subject_context": {
            "tenant_id": TENANT,
            "principal_id": PRINCIPAL,
            "workspace_id": None,
            "memory_context_version": "memory-context-v2",
            "memory_profile_id": None,
            "memory_profile_revision_id": None,
            "memory_profile_version": None,
        },
        "request_context": {
            "requested": {
                "workspace_supplied": False,
                "byte_budget": None,
                "token_budget": None,
                "item_budget": None,
            },
            "effective": {
                "workspace_id": None,
                "byte_budget": 4096,
                "token_budget": None,
                "item_budget": None,
            },
            "query_digest": None,
        },
        "decision_versions": {
            "scoring_version": "v1",
            "config_version": "v1",
            "candidate_strategy_version": "startup-candidates-v1",
            "manifest_contract_version": "context-manifest-v1",
            "packet_render_version": "working-set-v1",
        },
        "reorder_inputs": False,
    }


# Each mutation: (name, description, expected_error_substring, mutator).
# The expected_error substring must be a stable token the verifier emits; exact
# text need not match across languages, but each verifier reports the fixture
# name and the rejected invariant.
Mutator = Callable[[dict[str, Any]], None]


def _mutate_malformed_tenant_uuid(inp: dict[str, Any]) -> None:
    inp["subject_context"]["tenant_id"] = "not-a-uuid"


def _mutate_uppercase_tenant_uuid(inp: dict[str, Any]) -> None:
    inp["subject_context"]["tenant_id"] = "ABCDEF12-0000-0000-0000-000000000001"


def _mutate_malformed_item_uuid(inp: dict[str, Any]) -> None:
    inp["response"]["items"][0]["id"] = "not-a-uuid"


def _mutate_invalid_visibility(inp: dict[str, Any]) -> None:
    inp["response"]["items"][0]["visibility"] = "secret"


def _mutate_negative_item_count(inp: dict[str, Any]) -> None:
    inp["response"]["item_count"] = -1


def _mutate_negative_byte_count(inp: dict[str, Any]) -> None:
    inp["response"]["byte_count"] = -5


def _mutate_negative_omission_count(inp: dict[str, Any]) -> None:
    inp["response"]["omitted_count"] = -1


def _mutate_negative_requested_budget(inp: dict[str, Any]) -> None:
    inp["request_context"]["requested"]["byte_budget"] = -100


def _mutate_negative_effective_budget(inp: dict[str, Any]) -> None:
    inp["request_context"]["effective"]["byte_budget"] = -256


def _mutate_non_null_effective_startup_item_budget(inp: dict[str, Any]) -> None:
    inp["request_context"]["effective"]["item_budget"] = 5


def _mutate_profile_id_only(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_id"] = PROFILE


def _mutate_profile_revision_only(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_revision_id"] = PROFILE_REV


def _mutate_profile_version_only(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_version"] = 3


def _mutate_profile_id_revision_no_version(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_id"] = PROFILE
    inp["subject_context"]["memory_profile_revision_id"] = PROFILE_REV


def _mutate_profile_id_version_no_revision(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_id"] = PROFILE
    inp["subject_context"]["memory_profile_version"] = 3


def _mutate_profile_revision_version_no_id(inp: dict[str, Any]) -> None:
    inp["subject_context"]["memory_profile_revision_id"] = PROFILE_REV
    inp["subject_context"]["memory_profile_version"] = 3


def _mutate_malformed_sha256(inp: dict[str, Any]) -> None:
    # Startup query_digest must be null; setting a malformed digest triggers
    # both the query_digest-must-be-null startup rule AND the malformed-hash
    # rejection. The malformed hash is the stable invariant reported.
    inp["request_context"]["query_digest"] = "sha256:deadbeef"


def _mutate_uppercase_sha256(inp: dict[str, Any]) -> None:
    inp["request_context"]["query_digest"] = "sha256:" + "A" * 64


def _mutate_string_boolean(inp: dict[str, Any]) -> None:
    inp["response"]["items"][0]["pinned"] = "false"


def _mutate_mixed_type_reasons(inp: dict[str, Any]) -> None:
    inp["response"]["items"][0]["reasons"] = ["ok", 123]


MUTATIONS: list[tuple[str, str, str, Mutator]] = [
    (
        "malformed-tenant-uuid",
        "subject.tenant_id is a non-UUID string",
        "tenant_id",
        _mutate_malformed_tenant_uuid,
    ),
    (
        "uppercase-tenant-uuid",
        "subject.tenant_id is an uppercase (noncanonical) UUID",
        "tenant_id",
        _mutate_uppercase_tenant_uuid,
    ),
    (
        "malformed-item-uuid",
        "response item id is a non-UUID string",
        "item id",
        _mutate_malformed_item_uuid,
    ),
    (
        "invalid-visibility",
        "response item visibility is outside the storage vocabulary",
        "visibility",
        _mutate_invalid_visibility,
    ),
    (
        "negative-response-item-count",
        "response.item_count is negative",
        "item_count",
        _mutate_negative_item_count,
    ),
    (
        "negative-response-byte-count",
        "response.byte_count is negative",
        "byte_count",
        _mutate_negative_byte_count,
    ),
    (
        "negative-omission-count",
        "response.omitted_count is negative",
        "omitted_count",
        _mutate_negative_omission_count,
    ),
    (
        "negative-requested-budget",
        "requested.byte_budget is negative",
        "byte_budget",
        _mutate_negative_requested_budget,
    ),
    (
        "negative-effective-budget",
        "effective.byte_budget is negative",
        "byte_budget",
        _mutate_negative_effective_budget,
    ),
    (
        "non-null-effective-startup-item-budget",
        "effective.item_budget is non-null for startup v1",
        "item_budget",
        _mutate_non_null_effective_startup_item_budget,
    ),
    (
        "profile-id-only",
        "only memory_profile_id is set (partial profile)",
        "profile",
        _mutate_profile_id_only,
    ),
    (
        "profile-revision-only",
        "only memory_profile_revision_id is set (partial profile)",
        "profile",
        _mutate_profile_revision_only,
    ),
    (
        "profile-version-only",
        "only memory_profile_version is set (partial profile)",
        "profile",
        _mutate_profile_version_only,
    ),
    (
        "profile-id-revision-no-version",
        "memory_profile_id + revision_id without version (partial profile)",
        "profile",
        _mutate_profile_id_revision_no_version,
    ),
    (
        "profile-id-version-no-revision",
        "memory_profile_id + version without revision_id (partial profile)",
        "profile",
        _mutate_profile_id_version_no_revision,
    ),
    (
        "profile-revision-version-no-id",
        "revision_id + version without memory_profile_id (partial profile)",
        "profile",
        _mutate_profile_revision_version_no_id,
    ),
    (
        "malformed-sha256",
        "request_context.query_digest is a malformed sha256 (and non-null for startup)",
        "query_digest",
        _mutate_malformed_sha256,
    ),
    (
        "uppercase-sha256",
        "request_context.query_digest is an uppercase (noncanonical) sha256",
        "query_digest",
        _mutate_uppercase_sha256,
    ),
    (
        "string-boolean",
        "item.pinned is a string 'false' instead of a boolean",
        "pinned",
        _mutate_string_boolean,
    ),
    (
        "mixed-type-reasons",
        "item.reasons mixes strings and integers",
        "reasons",
        _mutate_mixed_type_reasons,
    ),
]


def _build_all() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for name, desc, expected_error, mutator in MUTATIONS:
        inp = _base_input()
        mutator(inp)
        fixtures.append(
            {
                "name": name,
                "mutation": desc,
                "expected_error": expected_error,
                "input": inp,
            }
        )
    return fixtures


def main() -> int:
    NEGATIVES_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = _build_all()
    for fixture in fixtures:
        path = NEGATIVES_DIR / f"{fixture['name']}.json"
        path.write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
        )
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"\nOK: {len(fixtures)} negative fixtures generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
