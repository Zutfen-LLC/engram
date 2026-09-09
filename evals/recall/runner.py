"""Run frozen semantic-recall replay through the shared production core."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engram import recall_shadow
from engram.demonstrated_usefulness import DEMONSTRATED_USEFULNESS_VERSION
from engram.embedding_profiles import get_active_profile
from engram.models import (
    AdmissionAssessment,
    AdmissionAssessmentCurrent,
    EmbeddingProfile,
    FeedbackEvent,
    KgTriple,
    MemoryAssessment,
    MemoryEdge,
    MemoryEmbedding,
    MemoryItem,
    RecallLog,
    TenantConfig,
    Tunnel,
)
from engram.recall_packing import RECALL_PACKING_VERSION
from engram.recall_profiles import (
    CERTIFIED_SERVING_PROFILES,
    RECALL_PROFILE_CONTRACT_VERSION,
)
from engram.recall_signals import RECALL_UTILITY_VERSION, SIGNALS_VERSION
from engram.semantic_budget import semantic_item_token_cost
from engram.semantic_context_manifest import SEMANTIC_MANIFEST_CONTRACT_VERSION
from evals.admission.schema import digest
from evals.recall.contracts import usefulness_perturbation_report
from evals.recall.metrics import build_profile_metrics
from evals.recall.schema import RecallEvaluationManifest

RUNNER_VERSION = "engram-recall-evaluation-runner-v1"
REPORT_SCHEMA_VERSION = "engram-recall-evaluation-report-v1"
_MUTATION_TABLES = (
    "memory_items",
    "recall_logs",
    "context_receipts",
    "feedback_events",
    "item_events",
    "admission_assessments",
    "admission_assessment_current",
)


def _canonical_value(value: Any) -> Any:
    """Convert ORM values to a deterministic, private digest representation."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_canonical_value(entry) for entry in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(entry) for key, entry in sorted(value.items())}
    # UUID, Decimal, and pgvector values all have stable textual forms.
    if value is not None and not isinstance(value, (str, int, float, bool)):
        return str(value)
    return value


def _row_identity(row: Any, *, excluded: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Capture every material stored field without retaining raw content."""
    return {
        column.name: _canonical_value(getattr(row, column.name))
        for column in row.__table__.columns
        if column.name not in excluded
    }


async def evaluation_state_identity(
    session: AsyncSession, manifest: RecallEvaluationManifest
) -> dict[str, Any]:
    """Return the replay-relevant live state sealed by ``snapshot_digest``.

    This is an explicit capture identity, not a claim that PostgreSQL can
    reconstruct a historical snapshot from an arbitrary timestamp.  A replay
    succeeds only while the connected state hashes to the frozen identity.
    Raw memory content, recall queries, and review notes never leave this
    function; memory content is represented by its stored content hash.
    """
    tenant_id = manifest.memory_context.tenant_id
    scoped_models = (
        MemoryItem,
        MemoryEmbedding,
        FeedbackEvent,
        RecallLog,
        MemoryAssessment,
        AdmissionAssessment,
        AdmissionAssessmentCurrent,
        KgTriple,
        MemoryEdge,
        Tunnel,
    )
    tables: dict[str, list[dict[str, Any]]] = {}
    for model in scoped_models:
        rows = list(
            (await session.scalars(select(model).where(model.tenant_id == tenant_id))).all()
        )
        excluded = (
            frozenset({"content", "query", "review_notes"})
            if model in (MemoryItem, RecallLog)
            else frozenset()
        )
        tables[model.__tablename__] = sorted(
            (_row_identity(row, excluded=excluded) for row in rows),
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
    config_rows = list(
        (
            await session.scalars(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
        ).all()
    )
    profiles = list((await session.scalars(select(EmbeddingProfile))).all())
    return {
        "schema_version": "engram-recall-evaluation-state-v1",
        "repository_sha": manifest.repository_sha,
        "runtime_versions": _runtime_versions(),
        "memory_context": manifest.memory_context.model_dump(mode="json"),
        "tenant_config": sorted(
            (_row_identity(row) for row in config_rows),
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
        "embedding_profiles": sorted(
            (_row_identity(row) for row in profiles),
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
        "tables": tables,
    }


def current_repository_sha() -> str:
    """Return the deployed source revision, or fail closed when unavailable."""
    configured = os.environ.get("ENGRAM_REPOSITORY_SHA")
    if configured:
        return configured
    repository = Path(__file__).resolve().parents[2]
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("recall_evaluation_repository_identity_unavailable") from error


async def _mutation_snapshot(session: AsyncSession) -> dict[str, int]:
    """Return bounded tenant-visible mutation counters for the proof record."""
    counts: dict[str, int] = {}
    for table in _MUTATION_TABLES:
        counts[table] = int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
    memory = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(recall_count), 0), "
                "COALESCE(sum(startup_recall_count), 0) FROM memory_items"
            )
        )
    ).one()
    counts["memory_items.recall_count_sum"] = int(memory[0])
    counts["memory_items.startup_recall_count_sum"] = int(memory[1])
    return counts


def _runtime_versions() -> dict[str, Any]:
    return {
        "recall_profile_contract_version": RECALL_PROFILE_CONTRACT_VERSION,
        "shadow_comparison_version": recall_shadow.SHADOW_COMPARISON_VERSION,
        "signals_version": SIGNALS_VERSION,
        "utility_version": RECALL_UTILITY_VERSION,
        "usefulness_version": DEMONSTRATED_USEFULNESS_VERSION,
        "packing_version": RECALL_PACKING_VERSION,
        "semantic_manifest_version": SEMANTIC_MANIFEST_CONTRACT_VERSION,
        "certified_serving_profiles": sorted(CERTIFIED_SERVING_PROFILES),
    }


def _safe_item(item: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded decision metadata while dropping raw memory content."""
    return {
        key: value
        for key, value in item.items()
        if key
        in {
            "id",
            "kind",
            "review_status",
            "score",
            "relevance_score",
            "utility_score",
            "warnings",
            "warning_codes",
            "epistemic_state",
            "evidence",
            "risk",
            "utility",
            "packing_reason",
            "relationship",
        }
    }


def _safe_packet(packet: dict[str, Any]) -> dict[str, Any]:
    token_count = sum(semantic_item_token_cost(item["content"]) for item in packet["items"])
    return {
        **{key: value for key, value in packet.items() if key != "items"},
        "items": [_safe_item(item) for item in packet["items"]],
        "token_count": token_count,
    }


def _concentration(packets: list[dict[str, Any]]) -> dict[str, Any]:
    exposures = Counter(str(item["id"]) for packet in packets for item in packet["items"])
    total = sum(exposures.values())
    counts = sorted(exposures.values())
    if not total:
        return {
            "exposure_count": 0,
            "distinct_items": 0,
            "top_1_share": None,
            "top_5_share": None,
            "top_10_share": None,
            "hhi": None,
            "gini": None,
        }
    shares = [count / total for count in counts]
    numerator = sum((2 * index - len(counts) - 1) * count for index, count in enumerate(counts, 1))
    return {
        "exposure_count": total,
        "distinct_items": len(counts),
        "top_1_share": max(counts) / total,
        "top_5_share": sum(counts[-5:]) / total,
        "top_10_share": sum(counts[-10:]) / total,
        "hhi": sum(share * share for share in shares),
        "gini": numerator / (len(counts) * total),
    }


def _strata(packets: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    fields = (
        "source_type",
        "kind",
        "review_status",
        "epistemic_state",
        "risk_state",
        "age_bucket",
        "usefulness_state",
    )
    result: dict[str, dict[str, int]] = {}
    for field in fields:
        counts = Counter(
            str(item[field])
            if field in item
            else str(item.get("evaluation_strata", {}).get(field, "unknown"))
            for packet in packets
            for item in packet["items"]
        )
        result[field] = dict(sorted(counts.items()))
    return result


async def _attach_item_strata(
    session: AsyncSession, case_rows: list[dict[str, Any]], evaluation_at: Any
) -> None:
    """Attach one bulk-read metadata projection for aggregate strata reporting."""
    item_ids = {
        item["id"]
        for row in case_rows
        for packet in [row["legacy"], *row["candidates"]]
        for item in packet["items"]
    }
    if not item_ids:
        return
    rows = (
        await session.execute(
            select(MemoryItem.id, MemoryItem.source_type, MemoryItem.created_at).where(
                MemoryItem.id.in_(item_ids)
            )
        )
    ).all()
    metadata = {
        str(item_id): {"source_type": source_type, "created_at": created_at}
        for item_id, source_type, created_at in rows
    }
    for row in case_rows:
        for packet in [row["legacy"], *row["candidates"]]:
            for item in packet["items"]:
                details = metadata.get(item["id"])
                age_hours = (
                    (evaluation_at - details["created_at"]).total_seconds() / 3600
                    if details is not None
                    else None
                )
                evidence = item.get("evidence") or {}
                usefulness = (item.get("utility") or {}).get("demonstrated_usefulness") or {}
                item["evaluation_strata"] = {
                    "source_type": details["source_type"] if details is not None else "unknown",
                    "risk_state": evidence.get("risk_state", "unknown"),
                    "age_bucket": _age_bucket(age_hours),
                    "usefulness_state": usefulness.get("state", "unknown"),
                }


def _age_bucket(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 24:
        return "lt24h"
    if age_hours < 72:
        return "24to72h"
    if age_hours < 168:
        return "72hto7d"
    if age_hours < 720:
        return "7dto30d"
    return "ge30d"


def _case_strata(case_rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Report all manifest strata with their sample counts."""
    fields = sorted({field for row in case_rows for field in row["strata"]})
    return {
        field: dict(
            sorted(Counter(row["strata"].get(field, "unknown") for row in case_rows).items())
        )
        for field in fields
    }


def _budget_utilization(packets: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate hard-budget use without retaining item content."""
    result: dict[str, Any] = {
        "item_count": sum(packet["item_count"] for packet in packets),
        "byte_count": sum(packet["byte_count"] for packet in packets),
        "token_count": sum(packet["token_count"] for packet in packets),
    }
    for measure, budget_key in (
        ("item", "effective_item_budget"),
        ("byte", "effective_byte_budget"),
        ("token", "effective_token_budget"),
    ):
        budget = sum(
            int(packet[budget_key]) for packet in packets if packet[budget_key] is not None
        )
        used = result[f"{measure}_count"]
        result[f"{measure}_budget"] = budget if budget else None
        result[f"{measure}_utilization"] = used / budget if budget else None
    return result


@contextmanager
def _statement_counter(session: AsyncSession) -> Any:
    """Measure statements issued by this evaluation transaction.

    The listener observes actual DBAPI executions.  It does not infer a query
    count from an expected code path.
    """
    count = {"total": 0}
    engine = session.sync_session.get_bind()

    def observe(*_args: Any, **_kwargs: Any) -> None:
        count["total"] += 1

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield count
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def _empty_packet(profile: str, case: Any) -> dict[str, Any]:
    """Represent a legitimate zero-eligible packet without provider work."""
    return {
        "profile": profile,
        "scoring_version": None,
        "signals_version": None,
        "item_count": 0,
        "byte_count": 0,
        "candidate_count": 0,
        "omitted_by_admission": {},
        "admission_diagnostics": [],
        "v2_resolution": None,
        "expansion": None,
        "packing": None,
        "effective_byte_budget": case.byte_budget,
        "effective_token_budget": case.token_budget,
        "effective_item_budget": case.item_budget,
        "items": [],
        "token_count": 0,
    }


def _public_report(
    manifest: RecallEvaluationManifest,
    case_rows: list[dict[str, Any]],
    *,
    mutation_proof: dict[str, Any],
) -> dict[str, Any]:
    profile_rows: dict[str, list[dict[str, Any]]] = {"governed": [], "exploratory": []}
    packets: dict[str, list[dict[str, Any]]] = {"legacy": [], "governed": [], "exploratory": []}
    for row in case_rows:
        packets["legacy"].append(row["legacy"])
        for candidate in row["candidates"]:
            packets[candidate["profile"]].append(candidate)
            profile_rows[candidate["profile"]].append(
                {
                    "legacy": row["legacy"],
                    "candidate": candidate,
                    "labels": row["labels"],
                }
            )
    profiles = {
        profile: {
            **build_profile_metrics(rows),
            "strata": _strata(packets[profile]),
            "budget_utilization": _budget_utilization(packets[profile]),
            "exposure_concentration": _concentration(packets[profile]),
        }
        for profile, rows in profile_rows.items()
    }
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "input": manifest.public_identity(),
        "input_digest": manifest.input_digest,
        "runtime_versions": _runtime_versions(),
        "profiles": profiles,
        "case_strata": _case_strata(case_rows),
        "legacy": {
            "strata": _strata(packets["legacy"]),
            "budget_utilization": _budget_utilization(packets["legacy"]),
            "exposure_concentration": _concentration(packets["legacy"]),
        },
        "read_only_proof": mutation_proof,
        "usefulness_perturbation": usefulness_perturbation_report(),
        "concentration_formula": {
            "hhi": "sum((item_exposures / total_exposures)^2)",
            "gini": "sum((2*i-n-1)*x_i) / (n*sum(x_i)), sorted x_i ascending",
        },
        # #198 does not contain deterministic certification gates.  The
        # runner can only report that it completed its evidence collection.
        "evaluation_status": "EVALUATION_COMPLETE",
        "limitations": [
            "Unknown labels stay unknown and are excluded from known-rate denominators.",
            "Usefulness is reported as utility evidence, not epistemic evidence.",
            "This report does not certify a serving profile.",
        ],
    }
    report["report_digest"] = digest(report)
    return report


async def run_recall_evaluation(
    session: AsyncSession, manifest: RecallEvaluationManifest
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate all profiles through the production shadow core.

    The caller must use :func:`read_only_evaluation_session`. This function
    rejects a writable transaction before it evaluates a packet.
    """
    read_only = await session.scalar(text("SHOW transaction_read_only"))
    if read_only != "on":
        raise ValueError("recall_evaluation_requires_read_only_transaction")
    active_embedding = await get_active_profile(session)
    if active_embedding.profile_key != manifest.embedding_profile_key:
        raise ValueError("embedding_profile_identity_mismatch")
    if current_repository_sha() != manifest.repository_sha:
        raise ValueError("repository_identity_mismatch")
    active_config = await session.scalar(
        select(TenantConfig).where(
            TenantConfig.tenant_id == manifest.memory_context.tenant_id,
            TenantConfig.active.is_(True),
        )
    )
    if active_config is None or active_config.config_version != manifest.tenant_config_version:
        raise ValueError("tenant_config_identity_mismatch")
    state = await evaluation_state_identity(session, manifest)
    if digest(state) != manifest.snapshot_digest:
        raise ValueError("evaluation_snapshot_identity_mismatch")
    before = await _mutation_snapshot(session)
    context = manifest.memory_context.resolve()
    case_rows: list[dict[str, Any]] = []
    timings_ms: list[float] = []
    case_query_counts: dict[str, int] = {}
    provider_calls = {
        "semantic_query_embedding": 0,
        "classification": 0,
        "assessment": 0,
        "usefulness": 0,
        "relationship_expansion": 0,
        "packing": 0,
        "other": 0,
    }
    with _statement_counter(session) as statement_count:
        for case in manifest.cases:
            before_case = statement_count["total"]
            started = time.perf_counter()
            result = await recall_shadow.evaluate_recall_shadow_comparison(
                session,
                memory_context=context,
                workspace=case.workspace,
                query=case.query,
                candidate_profiles=["governed", "exploratory"],
                byte_budget=case.byte_budget,
                token_budget=case.token_budget,
                item_budget=case.item_budget,
                now=manifest.evaluation_at,
            )
            timings_ms.append((time.perf_counter() - started) * 1000)
            case_query_counts[case.case_id] = statement_count["total"] - before_case
            if result.get("embedding_outcome") in {"succeeded", "disabled"}:
                provider_calls["semantic_query_embedding"] += 1
            if result["legacy"] is None:
                if result["candidate_count"] == 0 and context.may_read_anything:
                    legacy = _empty_packet("legacy", case)
                    candidates = [
                        _empty_packet("governed", case),
                        _empty_packet("exploratory", case),
                    ]
                elif result.get("embedding_outcome") == "disabled":
                    raise ValueError("recall_evaluation_query_embedding_unavailable")
                else:
                    raise ValueError("recall_evaluation_inaccessible_or_invalid_corpus")
            else:
                legacy = _safe_packet(result["legacy"])
                candidates = [_safe_packet(packet) for packet in result["candidates"]]
            case_rows.append(
                {
                    "case_id": case.case_id,
                    "query_digest": case.query_digest,
                    "strata": case.strata,
                    "labels": case.labels.model_dump(mode="json"),
                    "legacy": legacy,
                    "candidates": candidates,
                }
            )
        before_metadata = statement_count["total"]
        await _attach_item_strata(session, case_rows, manifest.evaluation_at)
        metadata_query_count = statement_count["total"] - before_metadata
    after = await _mutation_snapshot(session)
    if before != after:
        raise RuntimeError("recall_evaluation_detected_production_mutation")
    mutation_proof = {
        "transaction": "REPEATABLE READ READ ONLY",
        "snapshot_digest_verified": manifest.snapshot_digest,
        "before_after_equal": True,
        "tracked_tables": list(_MUTATION_TABLES),
        "tracked_counters": before,
        "total_db_statement_count": statement_count["total"],
        "case_db_statement_counts": case_query_counts,
        "metadata_query_count": metadata_query_count,
        "provider_calls": provider_calls,
        "candidate_receipts_persisted": 0,
    }
    public = _public_report(manifest, case_rows, mutation_proof=mutation_proof)
    private = {
        "private_report_schema_version": REPORT_SCHEMA_VERSION,
        "manifest": manifest.model_dump(mode="json"),
        "case_rows": case_rows,
        "public_report_digest": public["report_digest"],
        "performance": {
            "case_elapsed_ms": timings_ms,
            "p50_elapsed_ms": _percentile(timings_ms, 0.50),
            "p95_elapsed_ms": _percentile(timings_ms, 0.95),
            "timing_excluded_from_public_digest": True,
        },
    }
    return private, public


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


@asynccontextmanager
async def read_only_evaluation_session(
    session_factory: async_sessionmaker[AsyncSession], manifest: RecallEvaluationManifest
) -> AsyncIterator[AsyncSession]:
    """Open a tenant-scoped PostgreSQL transaction that rejects writes."""
    async with session_factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(manifest.memory_context.tenant_id)},
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', :principal_id, true)"),
            {"principal_id": str(manifest.memory_context.principal_id)},
        )
        yield session


def build_markdown_report(report: dict[str, Any]) -> str:
    """Render a deterministic public-safe completion report."""
    lines = [
        "# Recall shadow evaluation",
        "",
        f"- Input digest: `{report['input_digest']}`",
        f"- Repository SHA: `{report['input']['repository_sha']}`",
        f"- Cases: {report['input']['case_count']}",
        f"- Evaluation status: `{report['evaluation_status']}`",
        "- Privacy: aggregate-only; no memory or query content is included.",
        "",
        "## Packet comparison",
        "",
    ]
    for profile, metrics in report["profiles"].items():
        change = metrics["packet_change"]
        lines.extend(
            [
                f"### {profile}",
                "",
                f"- Membership-change rate: {change['membership_change_rate']}",
                f"- Ordering-only-change rate: {change['ordering_only_change_rate']}",
                f"- Mean Jaccard overlap: {change['mean_jaccard']}",
                f"- Contamination unknown: {metrics['contamination']['unknown']}",
                f"- Exposure HHI: {metrics['exposure_concentration']['hhi']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Read-only proof",
            "",
            "- Transaction: `REPEATABLE READ READ ONLY`.",
            "- Tracked mutation counters were equal before and after replay.",
            "- Candidate receipts were not persisted.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_exclusive(path: Path, payload: str, *, private: bool) -> None:
    target = path.resolve()
    repo = Path(__file__).resolve().parents[2]
    if private and target.is_relative_to(repo):
        raise ValueError("private_output_must_be_outside_repository")
    target.parent.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as output:
        output.write(payload)


def write_reports(
    *,
    private_path: Path | None,
    public_json_path: Path,
    public_markdown_path: Path,
    private: dict[str, Any],
    public: dict[str, Any],
) -> None:
    """Write public artifacts and an optional protected private artifact."""
    if private_path is not None:
        _write_exclusive(
            private_path,
            json.dumps(private, sort_keys=True, indent=2) + "\n",
            private=True,
        )
    _write_exclusive(
        public_json_path,
        json.dumps(public, sort_keys=True, indent=2) + "\n",
        private=False,
    )
    _write_exclusive(public_markdown_path, build_markdown_report(public), private=False)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "RUNNER_VERSION",
    "build_markdown_report",
    "current_repository_sha",
    "evaluation_state_identity",
    "read_only_evaluation_session",
    "run_recall_evaluation",
    "write_reports",
]
