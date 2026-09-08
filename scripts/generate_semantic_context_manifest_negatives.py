"""Generate negative semantic manifest conformance fixtures."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "semantic-context-manifest-v1" / "vectors"
OUT = ROOT / "conformance" / "semantic-context-manifest-v1" / "negative"


def load(name: str) -> dict[str, Any]:
    return json.loads((VECTORS / name).read_text())


def at(value: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    target: Any = value
    for part in path:
        target = target[part]
    return target


def set_value(path: tuple[str | int, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(manifest: dict[str, Any]) -> None:
        at(manifest, path[:-1])[path[-1]] = value

    return mutate


def add(path: tuple[str | int, ...], key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(manifest: dict[str, Any]) -> None:
        at(manifest, path)[key] = value

    return mutate


def delete(path: tuple[str | int, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(manifest: dict[str, Any]) -> None:
        del at(manifest, path[:-1])[path[-1]]

    return mutate


def write(
    name: str,
    vector: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None],
    *,
    parse_as: str = "semantic",
) -> None:
    manifest = deepcopy(vector["expected"]["manifest"])
    mutate(manifest)
    fixture = {
        "name": name,
        "parse_as": parse_as,
        "input": vector["input"],
        "manifest": manifest,
    }
    (OUT / f"{name}.json").write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, allow_nan=True) + "\n"
    )


def add_legacy_v2_item(governed: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Copy one otherwise-valid candidate item projection onto a legacy item."""

    candidate = governed["expected"]["manifest"]["items"][0]

    def mutate(manifest: dict[str, Any]) -> None:
        item = manifest["items"][0]
        for field in (
            "admission",
            "evidence",
            "relevance_score",
            "utility_score",
            "packing_reason",
        ):
            item[field] = deepcopy(candidate[field])

    return mutate


def set_mirrored_v2_field(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Set one V2 fact in both receipt projections so only its vocabulary is invalid."""

    def mutate(manifest: dict[str, Any]) -> None:
        item = manifest["items"][0]
        item["admission"]["v2"]["fresh"][field] = value
        item["evidence"][field] = value

    return mutate


def set_mirrored_assessment_ref_field(field: str, value: str) -> Callable[[dict[str, Any]], None]:
    """Set one assessment-ref fact in both receipt projections."""

    def mutate(manifest: dict[str, Any]) -> None:
        item = manifest["items"][0]
        item["admission"]["v2"]["fresh"]["effective_assessment_refs"][0][field] = value
        item["evidence"]["effective_assessment_refs"][0][field] = value

    return mutate


def set_mirrored_profile_key(value: str) -> Callable[[dict[str, Any]], None]:
    """Set the V2 profile key in both receipt projections."""

    def mutate(manifest: dict[str, Any]) -> None:
        item = manifest["items"][0]
        item["admission"]["v2"]["profile_key"] = value
        item["evidence"]["profile_key"] = value

    return mutate


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    legacy = load("001-legacy-single.json")
    multi = load("002-legacy-multi.json")
    governed = load("007-governed-v2.json")
    relationship = load("008-relationship.json")
    startup = json.loads(
        (
            ROOT / "conformance" / "context-manifest-v1" / "vectors" / "001-empty-startup.json"
        ).read_text()
    )

    cases = [
        ("raw-query-injected", legacy, add(("result",), "raw_query", "alpha query"), "semantic"),
        ("raw-content-injected", legacy, add(("items", 0), "content", "alpha"), "semantic"),
        ("extra-top-level", legacy, add((), "unexpected", True), "semantic"),
        (
            "extra-admission-field",
            governed,
            add(("items", 0, "admission"), "raw_query", "x"),
            "semantic",
        ),
        (
            "extra-evidence-field",
            governed,
            add(("items", 0, "evidence"), "provider_output", "x"),
            "semantic",
        ),
        (
            "extra-relationship-field",
            relationship,
            add(("items", 0, "relationship"), "future", 1),
            "semantic",
        ),
        ("extra-packing-field", governed, add(("result", "packing"), "future", 1), "semantic"),
        (
            "extra-expansion-field",
            relationship,
            add(("result", "expansion"), "future", 1),
            "semantic",
        ),
        (
            "uppercase-uuid",
            legacy,
            set_value(("subject", "tenant_id"), "00000000-0000-0000-0000-00000000000A"),
            "semantic",
        ),
        ("malformed-uuid", legacy, set_value(("items", 0, "item_id"), "not-a-uuid"), "semantic"),
        ("malformed-digest", legacy, set_value(("packet", "hash"), "sha256:no"), "semantic"),
        (
            "uppercase-digest",
            legacy,
            set_value(("packet", "hash"), "sha256:" + "A" * 64),
            "semantic",
        ),
        ("nan", legacy, set_value(("items", 0, "score"), math.nan), "semantic"),
        ("positive-infinity", legacy, set_value(("items", 0, "score"), math.inf), "semantic"),
        ("negative-infinity", legacy, set_value(("items", 0, "score"), -math.inf), "semantic"),
        ("unsupported-schema-version", legacy, set_value(("schema_version",), "2.0"), "dispatch"),
        (
            "unsupported-contract-version",
            legacy,
            set_value(("versions", "manifest_contract_version"), "semantic-context-manifest-v2"),
            "dispatch",
        ),
        ("unknown-schema", legacy, set_value(("schema",), "engram.unknown"), "dispatch"),
        (
            "startup-as-semantic",
            {"input": legacy["input"], "expected": startup["expected"]},
            lambda _: None,
            "semantic",
        ),
        ("semantic-as-startup", legacy, lambda _: None, "startup"),
        ("reordered-ordinals", multi, set_value(("items", 0, "ordinal"), 1), "semantic"),
        ("changed-packet-order", multi, lambda value: value["items"].reverse(), "semantic"),
        (
            "tampered-query-digest",
            legacy,
            set_value(("request", "query_digest"), "sha256:" + "0" * 64),
            "semantic",
        ),
        (
            "tampered-request-digest",
            legacy,
            set_value(("request", "request_digest"), "sha256:" + "0" * 64),
            "semantic",
        ),
        (
            "tampered-served-content-hash",
            legacy,
            set_value(("items", 0, "served_content_hash"), "sha256:" + "0" * 64),
            "semantic",
        ),
        (
            "incomplete-v2-evidence",
            governed,
            delete(("items", 0, "evidence", "decision_hash")),
            "semantic",
        ),
        (
            "mismatched-v2-evidence",
            governed,
            set_value(("items", 0, "evidence", "decision_hash"), "sha256:" + "0" * 64),
            "semantic",
        ),
        (
            "packing-selected-count",
            governed,
            set_value(("result", "packing", "selected_count"), 2),
            "semantic",
        ),
        (
            "wrong-relationship-contract",
            relationship,
            set_value(("items", 0, "relationship", "version"), "relationship-relevance-v2"),
            "semantic",
        ),
        (
            "invalid-risk-state",
            governed,
            set_mirrored_v2_field("risk_state", "probably_safe"),
            "semantic",
        ),
        (
            "invalid-retention-state",
            governed,
            set_mirrored_v2_field("retention_state", "forever"),
            "semantic",
        ),
        (
            "invalid-admission-tier",
            governed,
            set_value(
                ("items", 0, "admission", "v2", "fresh", "highest_admission_tier"),
                "super_trusted",
            ),
            "semantic",
        ),
        (
            "invalid-assertion-mode",
            governed,
            set_mirrored_assessment_ref_field("assertion_mode", "explicit"),
            "semantic",
        ),
        (
            "invalid-origin",
            governed,
            set_mirrored_assessment_ref_field("origin", "external"),
            "semantic",
        ),
        (
            "invalid-next-action",
            governed,
            set_value(
                ("items", 0, "admission", "v2", "fresh", "next_actions"),
                ["retry_later"],
            ),
            "semantic",
        ),
        (
            "invalid-assessment-outcome",
            governed,
            set_value(("items", 0, "admission", "assessment_outcome"), "qualified"),
            "semantic",
        ),
        (
            "invalid-profile-key",
            governed,
            set_mirrored_profile_key("future_profile"),
            "semantic",
        ),
        (
            "invalid-warning-code",
            governed,
            set_value(("items", 0, "warning_codes"), ["probably_safe"]),
            "semantic",
        ),
        (
            "invalid-review-status",
            legacy,
            set_value(("items", 0, "review_status"), "pending_forever"),
            "semantic",
        ),
        (
            "invalid-conflict-type",
            legacy,
            set_value(("items", 0, "conflict_type"), "ambiguous"),
            "semantic",
        ),
        (
            "invalid-conflict-resolution-status",
            legacy,
            set_value(("items", 0, "conflict_resolution_status"), "ignored"),
            "semantic",
        ),
        (
            "invalid-packing-reason",
            governed,
            set_value(("items", 0, "packing_reason"), "lucky"),
            "semantic",
        ),
        (
            "invalid-relationship-origin",
            relationship,
            set_value(("items", 0, "relationship", "origins"), ["semantic", "future"]),
            "semantic",
        ),
        (
            "legacy-with-admission-policy",
            legacy,
            set_value(("versions", "admission_policy"), "recall-admission-v2"),
            "semantic",
        ),
        (
            "legacy-with-packing-version",
            legacy,
            set_value(("versions", "packing_version"), "recall-packing-v1"),
            "semantic",
        ),
        ("legacy-with-v2-item", legacy, add_legacy_v2_item(governed), "semantic"),
        (
            "governed-without-admission-policy",
            governed,
            set_value(("versions", "admission_policy"), None),
            "semantic",
        ),
        (
            "governed-without-packing-version",
            governed,
            set_value(("versions", "packing_version"), None),
            "semantic",
        ),
    ]
    for name, vector, mutation, parse_as in cases:
        write(name, vector, mutation, parse_as=parse_as)


if __name__ == "__main__":
    main()
