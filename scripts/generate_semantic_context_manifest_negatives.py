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
    ]
    for name, vector, mutation, parse_as in cases:
        write(name, vector, mutation, parse_as=parse_as)


if __name__ == "__main__":
    main()
