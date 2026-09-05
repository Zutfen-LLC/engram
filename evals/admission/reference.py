"""Generate the field and enum reference from the validated contract set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.admission.dataset import Dataset
from evals.admission.schema import LabelRecord, Manifest


def reference(dataset: Dataset) -> str:
    lines = [
        "# Admission v1 field reference",
        "",
        "Read this reference with [the labeling handbook](admission-v1.md).",
        "",
        "All objects reject unlisted fields. All listed fields are required unless",
        "the table marks them optional. Null and unknown are distinct values.",
        "",
        "Reserved enum values have no synthetic sample coverage in v1. They remain",
        "available for real-data adjudication. Contract tests exercise unknown,",
        "dual review, unresolved disagreement, and resolution separately.",
        "",
    ]
    observed: dict[str, set[str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str):
                    observed.setdefault(key, set()).add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for sample in dataset.samples:
        if sample.label:
            visit(sample.label.model_dump(mode="json"))
    visit(dataset.manifest.model_dump(mode="json"))
    for model in (LabelRecord, Manifest):
        schema = model.model_json_schema()
        for name, definition in [(model.__name__, schema), *schema.get("$defs", {}).items()]:
            lines += [
                f"## {name}",
                "",
                "| Field | Required | Type / allowed values | Reserved |",
                "| --- | --- | --- | --- |",
            ]
            for field, spec in definition["properties"].items():

                def describe(part: dict[str, Any]) -> str:
                    if "$ref" in part:
                        return str(part["$ref"]).split("/")[-1]
                    if "enum" in part:
                        return ", ".join(part["enum"])
                    if "const" in part:
                        return str(part["const"])
                    if "anyOf" in part:
                        return " or ".join(describe(p) for p in part["anyOf"])
                    if part.get("type") == "array":
                        return "array of " + describe(part.get("items", {}))
                    return str(part.get("format", part.get("type", "tuple")))

                reserved = sorted(set(spec.get("enum", [])) - observed.get(field, set()))
                lines.append(
                    f"| `{field}` | {'yes' if field in definition.get('required', []) else 'no'}"
                    f" | {describe(spec)} | {', '.join(reserved) or '—'} |"
                )
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    root = Path(__file__).parent.parent
    data = Dataset.model_validate_json((root / "admission/contract-v1.json").read_bytes())
    (root / "labeling/field-reference-v1.md").write_text(reference(data))
    (root / "admission/sampling-v1.json").write_text(
        json.dumps(
            {
                "selection_method": "census",
                "selection_seed": "dogfood-baseline-v1",
                "strata": [
                    "source_type",
                    "kind",
                    "review_status",
                    "blocker",
                    "evidence_state",
                    "selected_lane",
                    "age_bucket",
                    "conflict",
                    "dispute",
                    "recalled",
                ],
                "per_stratum": 10,
                "excluded_strata": [],
            },
            indent=2,
        )
        + "\n"
    )
