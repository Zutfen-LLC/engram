"""Unit tests for the human-readable usage-report renderer (ENG-METER-001).

These tests do NOT require a live database — they invoke
``_print_human_usage_report`` directly with a synthetic report dict, so the
renderer is covered even in environments without PostgreSQL. The renderer
previously crashed with ``KeyError: 'fallbacks'`` because the builder renamed
the column to ``application_fallbacks``; this guards against that class of
drift.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engram.cli import _print_human_usage_report


def _sample_report(*, with_provider_row: bool = True) -> dict[str, Any]:
    """A minimal report dict with every key the renderer reads."""
    report: dict[str, Any] = {
        "report_schema_version": 3,
        "tenant_id": None,
        "since": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
        "until": datetime(2026, 7, 2, tzinfo=UTC).isoformat(),
        "coverage": {
            "telemetry_enabled": True,
            "first_event_at": None,
            "last_event_at": None,
            "pct_provider_calls_with_tokens": 0.0,
            "pct_provider_calls_with_cost": 0.0,
            "active_principals": 0,
            "active_principals_with_lifecycle_summary": 0,
            "warnings": [],
        },
        "candidate_funnel": {
            "lifecycle_extracted": 0,
            "lifecycle_guard_rejected": 0,
            "lifecycle_classified": 0,
            "lifecycle_parked": 0,
            "candidate_observations": 8,
            "candidate_cohort_size": 8,
            "logical_candidates": 7,
            "unresolved_candidates": 1,
            "remember_attempts": 9,
            "created": 4,
            "deduped": 2,
            "superseded": 0,
            "failed": 0,
            "new_memory_writes": 4,
            "failed_attempts": 2,
            "successful_attempts": 7,
            "retry_successes_in_window": 1,
            "attempts_per_cohort_candidate_avg": 1.12,
            "flat_candidate_units": 0,
            "kib_candidate_units": 0,
            "candidate_bytes_p50": 0,
            "candidate_bytes_p90": 0,
            "candidate_bytes_p99": 0,
        },
        "by_source_type": [],
        "provider_economics": [],
        "all_provider_operations": 10,
        "all_actual_provider_calls": 9,
        "all_non_attempted_failures": 1,
        "all_disabled_operations": 1,
        "product_provider_operations": 10,
        "product_actual_provider_calls": 9,
        "maintenance_provider_operations": 0,
        "maintenance_actual_provider_calls": 0,
        "diagnostic_provider_operations": 0,
        "diagnostic_actual_provider_calls": 0,
        "conflict_economics": {
            "conflict_classifications": 3,
            "conflict_actual_calls": 2,
            "conflict_calls_per_1000_candidate_observations": 250.0,
            "verdict_distribution": {},
            "failed_or_fallback_count": 0,
            "failed_calls": 0,
            "application_fallback_count": 0,
        },
        "retrieval": {
            "by_mode": [],
            "total_requests": 0,
            "query_embedding_calls": 5,
            "query_embedding_actual_calls": 4,
            "query_embedding_tokens": 123,
            "semantic_queries_per_created_memory": 0.0,
            "semantic_queries_per_new_memory_write": 0.0,
            "retrievals_per_active_principal": 0.0,
        },
        "worker": {"by_job_type_status": [], "oldest_pending_age_seconds": None},
        "storage": {
            "memory_items_total": 0,
            "memory_items_live": 0,
            "memory_items_active": 0,
            "memory_items_proposed": 0,
            "memory_items_disputed": 0,
            "memory_items_rejected": 0,
            "memory_items_archived": 0,
            "embeddings_ready": 0,
            "embeddings_pending": 0,
            "embeddings_failed": 0,
            "embedding_profiles_total": 0,
            "embedding_profiles_writable": 0,
            "database_bytes": 0,
            "bytes_per_retained_memory": None,
            "bytes_per_ready_embedding": None,
        },
        "hourly_series": [],
    }
    if with_provider_row:
        report["provider_economics"] = [
            {
                "operation": "classification",
                "usage_class": "request",
                "provider_host": "api.openai.com",
                "model": "gpt-4o-mini",
                "calls": 10,
                "actual_calls": 9,
                "successes": 8,
                "failures": 2,
                "disabled_n": 1,
                "application_fallbacks": 2,
                "input_count": 10,
                "prompt_tokens": 500,
                "completion_tokens": 100,
                "total_tokens": 600,
                "reported_cost_usd": 0.0123,
                "with_reported_cost": 10,
                "reported_cost_coverage_pct": 100.0,
                "latency_p50": 200,
                "latency_p90": 400,
                "latency_p99": 900,
            }
        ]
    return report


def test_human_report_renders_without_error_with_provider_row():
    """The renderer must not raise when provider_economics has a row.

    Regression guard: the builder renamed ``fallbacks`` to
    ``application_fallbacks`` and added ``disabled_n``, but the renderer
    previously read ``row['fallbacks']`` — a KeyError.
    """
    report = _sample_report(with_provider_row=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_human_usage_report(report)
    output = buf.getvalue()
    golden = Path("tests/fixtures/usage_report_human_golden.txt").read_text()
    assert output == golden
    assert "classification" in output
    # The renamed column is rendered (as the fallback count).
    assert "fallback=2" in output
    # The new disabled count is rendered.
    assert "operations=10" in output
    assert "calls=9" in output
    assert "disabled=1" in output
    assert "actual conflict LLM calls" in output
    assert "actual query-embedding calls" in output
    assert "logical_candidates" in output
    assert "remember_attempts" in output


def test_human_report_renders_without_provider_rows():
    """The renderer must not raise when provider_economics is empty."""
    report = _sample_report(with_provider_row=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_human_usage_report(report)
    output = buf.getvalue()
    assert "Engram dogfood usage report" in output
