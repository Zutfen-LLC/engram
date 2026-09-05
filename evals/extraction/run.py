"""Run the frozen extraction golden set through the API on Compose PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any

import httpx


def score_case(case: dict[str, Any], response: dict[str, Any], status: int) -> dict[str, Any]:
    """Match explicit lexical criteria without using a second model as a judge."""
    receipt = response.get("receipt", {})
    emitted = receipt.get("candidates", [])
    candidates = [c for c in emitted if c["outcome"] not in {"abstained", "rejected", "error"}]
    expected = case["expected"]
    used: set[int] = set()
    matched = attribution = kind = retention = evidence = cues = 0
    for target in expected:
        for i, candidate in enumerate(candidates):
            if i in used or not all(
                re.search(pattern, candidate["content"], re.IGNORECASE)
                for pattern in target["match_all"]
            ):
                continue
            used.add(i)
            matched += 1
            attribution += int(
                candidate["assertion_mode"] == target["assertion_mode"]
                and candidate["asserting_role"] == target["role"]
            )
            kind += int(candidate["suggested_kind"] == target["kind"])
            retention += int(candidate["retention_disposition"] == target["retention"])
            evidence += int(
                set(target["evidence_ids"]) <= {s["message_id"] for s in candidate["evidence"]}
            )
            cues += int(
                set(target["cue_types"]) <= {c["cue_type"] for c in candidate["source_cues"]}
            )
            break
    by_id = {m["message_id"]: m for m in case["messages"]}
    spans = [s for c in emitted for s in c["evidence"]]
    valid = sum(
        s["message_id"] in by_id
        and 0 <= s["start"] < s["end"] <= len(by_id[s["message_id"]]["content"])
        for s in spans
    )
    return {
        "case_id": case["id"],
        "http_status": status,
        "provider_request": status != 422,
        "status_ok": status == case.get("expected_status", 200),
        "expected": len(expected),
        "emitted": len(candidates),
        "matched": matched,
        "attribution_correct": attribution,
        "kind_correct": kind,
        "retention_correct": retention,
        "evidence_coverage_correct": evidence,
        "cue_coverage_correct": cues,
        "spans": len(spans),
        "valid_spans": valid,
        "duplicate_candidates": len(candidates) - len({c["content"] for c in candidates}),
        "abstained": not candidates,
        "latency_ms": receipt.get("latency_ms"),
        "input_tokens": receipt.get("input_tokens"),
        "output_tokens": receipt.get("output_tokens"),
        "provider_cost_usd": receipt.get("provider_cost_usd"),
        "provider": receipt.get("provider"),
        "model": receipt.get("model"),
        "provider_model": receipt.get("provider_model"),
        "response": response,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> int:
        return sum(r[key] for r in rows)

    def ratio(key: str, denominator: int) -> float | None:
        return total(key) / denominator if denominator else None

    matched = total("matched")
    provider_requests = total("provider_request")
    precision = ratio("matched", total("emitted"))
    recall = ratio("matched", total("expected"))
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    costs = [r["provider_cost_usd"] for r in rows if r["provider_cost_usd"] is not None]
    return {
        "cases": len(rows),
        "status_failures": len(rows) - total("status_ok"),
        "candidate_precision": precision,
        "candidate_f1": (
            2 * precision * recall / (precision + recall) if precision and recall else 0.0
        ),
        "candidate_recall": ratio("matched", total("expected")),
        "attribution_accuracy": ratio("attribution_correct", matched),
        "kind_accuracy": ratio("kind_correct", matched),
        "retention_label_accuracy": ratio("retention_correct", matched),
        "evidence_span_validity": ratio("valid_spans", total("spans")),
        "evidence_coverage": ratio("evidence_coverage_correct", matched),
        "explicit_cue_coverage": ratio("cue_coverage_correct", matched),
        "duplicate_rate": ratio("duplicate_candidates", total("emitted")),
        "abstention_rate": ratio("abstained", len(rows)),
        "latency_ms_median": statistics.median(latencies) if latencies else None,
        "latency_ms_max": max(latencies) if latencies else None,
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "reported_cost_usd": sum(costs) if len(costs) == provider_requests and costs else None,
        "known_reported_cost_usd": sum(costs),
        "cost_reporting_coverage": len(costs) / provider_requests if provider_requests else None,
    }


async def run(args: argparse.Namespace) -> None:
    # Configuration must be loaded before importing the service settings and engines.
    if args.env_file:
        from dotenv import dotenv_values

        for name, value in dotenv_values(args.env_file).items():
            if (
                name.startswith("ENGRAM_")
                and value
                and not any(
                    word in name for word in ("DATABASE", "AUTH", "PROVISION", "DELEGATION")
                )
            ):
                os.environ[name] = value
    if not os.environ.get("ENGRAM_APP_DATABASE_URL"):
        raise RuntimeError("run this evaluation in the Compose PostgreSQL stack")
    os.environ["ENGRAM_DATABASE_URL"] = os.environ["ENGRAM_APP_DATABASE_URL"]
    os.environ["ENGRAM_AUTH_ENABLED"] = "false"
    from engram.api.app import create_app
    from engram.provider_clients import resolve_classification_provider

    provider = resolve_classification_provider()
    if provider.provider_adapter != "openai" or not provider.api_key:
        raise RuntimeError("configured classification provider is unavailable")
    raw = Path(args.golden).read_bytes()
    golden = json.loads(raw)
    rows = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://extraction-eval",
        timeout=45,
    ) as client:
        for case in golden["cases"]:
            response = await client.post(
                "/v1/extract",
                json={
                    "messages": case["messages"],
                    "mode": "preview",
                    "source_type": "extraction",
                },
            )
            rows.append(score_case(case, response.json(), response.status_code))
            print(f"{case['id']}: HTTP {response.status_code}", flush=True)
    report = {
        "schema_version": "engram.extraction-evaluation.v1",
        "mode": "live_provider_api",
        "golden_sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version_evaluated": "engram.extraction.v1",
        "prompt_version": "engram.extract.3",
        "measurement": "Frozen lexical criteria; attribution, kind, retention and spans separate.",
        "limitations": [
            "Small synthetic set; no production certification.",
            "Lexical matching can reject valid paraphrases.",
            "Unknown provider cost remains null; tokens are measured separately.",
        ],
        "metrics": aggregate(rows),
        "cases": rows,
    }
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", default="evals/extraction/golden-v1.json")
    parser.add_argument("--output", default="evals/extraction/live-v1.json")
    parser.add_argument("--env-file")
    asyncio.run(run(parser.parse_args()))
