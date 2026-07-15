"""Dogfood usage report (ENG-METER-001 / ENG-METER-002) — ``engram usage-report``.

Derives diagnostic candidate-funnel, provider-economics, retrieval, worker,
and storage statistics from the append-only ``usage_events`` ledger plus
existing tables (jobs, memory_items, memory_embeddings, embedding_profiles).

This is an OBSERVABILITY report, not an invoice: ``flat_candidate_units`` and
``kib_candidate_units`` are hypothetical meter scenarios for analysis only,
never authoritative billable usage. Client-reported lifecycle summaries
(``client.lifecycle_summary``) are diagnostic and untrusted — never treated as
ground truth for candidates the server itself observed.

Percentiles use PostgreSQL's ``percentile_cont`` so no unbounded event history
is loaded into Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings

REPORT_SCHEMA_VERSION = 4


@dataclass
class ReportWindow:
    tenant_id: str | None
    since: datetime
    until: datetime


def default_window(
    *, tenant: str | None, since: datetime | None, until: datetime | None
) -> ReportWindow:
    now = datetime.now(UTC)
    return ReportWindow(
        tenant_id=tenant,
        since=since if since is not None else now - timedelta(days=7),
        until=until if until is not None else now,
    )


def _tenant_clause(window: ReportWindow, *, alias: str = "") -> tuple[str, dict[str, Any]]:
    prefix = f"{alias}." if alias else ""
    params: dict[str, Any] = {"since": window.since, "until": window.until}
    clause = f"{prefix}created_at >= :since AND {prefix}created_at < :until"
    if window.tenant_id is not None:
        clause += f" AND {prefix}tenant_id = :tenant_id"
        params["tenant_id"] = window.tenant_id
    return clause, params


async def _scalar(session: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]


def _candidate_cohort_cte() -> str:
    """Reusable cohort-first, as-of-until logical candidate SQL."""
    return """
        candidate_first_seen AS (
            SELECT tenant_id,
                   COALESCE('ingest:' || ingest_id::text,
                            'legacy:' || correlation_id::text) AS candidate_key,
                   min(created_at) AS first_seen_at
            FROM usage_events
            WHERE event_type IN ('candidate.observed', 'candidate.outcome')
              AND (ingest_id IS NOT NULL OR correlation_id IS NOT NULL)
              AND (CAST(:tenant_id AS uuid) IS NULL
                   OR tenant_id = CAST(:tenant_id AS uuid))
            GROUP BY tenant_id, candidate_key
        ),
        candidate_cohort AS (
            SELECT tenant_id, candidate_key, first_seen_at
            FROM candidate_first_seen
            WHERE first_seen_at >= :since AND first_seen_at < :until
        ),
        ranked_candidate_outcomes AS (
            SELECT ue.tenant_id, cc.candidate_key, ue.principal_id, ue.status,
                   ue.created_at, ue.id, cc.first_seen_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY ue.tenant_id, cc.candidate_key
                       ORDER BY CASE WHEN ue.status != 'failed' THEN 0 ELSE 1 END,
                                ue.created_at, ue.id
                   ) AS logical_rank
            FROM candidate_cohort cc
            JOIN usage_events ue
              ON ue.tenant_id = cc.tenant_id
             AND COALESCE('ingest:' || ue.ingest_id::text,
                          'legacy:' || ue.correlation_id::text) = cc.candidate_key
             AND ue.event_type = 'candidate.outcome'
             AND ue.created_at < :until
        ),
        logical_candidate_outcomes AS (
            SELECT tenant_id, candidate_key, principal_id, status, created_at, first_seen_at
            FROM ranked_candidate_outcomes
            WHERE logical_rank = 1
        )
    """


def _cohort_params(window: ReportWindow) -> dict[str, Any]:
    return {"since": window.since, "until": window.until, "tenant_id": window.tenant_id}


async def _coverage_section(session: AsyncSession, window: ReportWindow) -> dict[str, Any]:
    clause, params = _tenant_clause(window)
    first_last = (
        await session.execute(
            text(f"SELECT min(created_at) AS first_ts, max(created_at) AS last_ts "
                 f"FROM usage_events WHERE {clause}"),
            params,
        )
    ).mappings().one()

    provider_clause = clause + " AND event_type = 'provider.call'"
    provider_coverage = (
        await session.execute(
            text(
                f"SELECT "
                f"count(*) AS total, "
                f"count(*) FILTER (WHERE external_call_attempted IS TRUE) AS actual_calls, "
                f"count(*) FILTER (WHERE status = 'disabled') AS disabled_calls, "
                f"count(*) FILTER (WHERE status = 'failed' "
                f"AND external_call_attempted IS FALSE) AS non_attempted_failures, "
                f"count(*) FILTER (WHERE external_call_attempted IS TRUE "
                f"AND total_tokens IS NOT NULL) "
                f"AS with_tokens, "
                f"count(*) FILTER (WHERE external_call_attempted IS TRUE "
                f"AND reported_cost_usd IS NOT NULL) "
                f"AS with_cost "
                f"FROM usage_events WHERE {provider_clause}"
            ),
            params,
        )
    ).mappings().one()
    candidate_coverage = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE ingest_id IS NOT NULL) AS with_ingest, "
                "count(*) FILTER (WHERE ingest_id IS NULL AND correlation_id IS NOT NULL) "
                "AS legacy FROM usage_events WHERE "
                + clause
                + " AND event_type IN ('candidate.observed', 'candidate.outcome')"
            ),
            params,
        )
    ).mappings().one()

    # ``disabled`` is NOT an external provider call (the provider is ``none``),
    # so it is excluded from the actual-call and usage-coverage denominators —
    # otherwise a deployment with the embedding provider off would see its token
    # coverage ratio dragged toward zero by rows that never called a provider
    # (ENG-METER-001 blocking correction).
    total_calls = provider_coverage["total"] or 0
    actual_calls = provider_coverage["actual_calls"] or 0
    disabled_calls = provider_coverage["disabled_calls"] or 0
    pct_tokens = (
        round(100.0 * provider_coverage["with_tokens"] / actual_calls, 1)
        if actual_calls
        else 0.0
    )
    pct_cost = (
        round(100.0 * provider_coverage["with_cost"] / actual_calls, 1)
        if actual_calls
        else 0.0
    )

    active_principals = await _scalar(
        session,
        f"SELECT count(DISTINCT principal_id) FROM usage_events "
        f"WHERE {clause} AND principal_id IS NOT NULL",
        params,
    )
    principals_with_summary = await _scalar(
        session,
        f"SELECT count(DISTINCT principal_id) FROM usage_events "
        f"WHERE {clause} AND event_type = 'client.lifecycle_summary' "
        f"AND principal_id IS NOT NULL",
        params,
    )
    active_principals = active_principals or 0
    principals_with_summary = principals_with_summary or 0
    principals_without_summary = max(0, active_principals - principals_with_summary)

    warnings: list[str] = []
    if not settings.usage_telemetry_enabled:
        warnings.append(
            "ENGRAM_USAGE_TELEMETRY_ENABLED is false on this process — figures below "
            "reflect whatever was collected while it was enabled, if anything."
        )
    if first_last["first_ts"] is None:
        warnings.append("no usage_events rows found in the requested window")
    if actual_calls > 0 and pct_tokens < 50.0:
        warnings.append(
            f"only {pct_tokens}% of actual (non-disabled) provider.call events "
            "carry token usage — provider/proxy may not be returning usage data"
        )
    if disabled_calls:
        warnings.append(
            f"{disabled_calls} provider.call event(s) are 'disabled' (no external "
            "call occurred) and excluded from the actual-call and usage-coverage "
            "denominators"
        )
    if principals_without_summary > 0:
        warnings.append(
            f"{principals_without_summary} active principal(s) have no client "
            "lifecycle summaries — locally guard-rejected/parked candidates for "
            "those principals are invisible to this report"
        )
    candidate_total = int(candidate_coverage["total"] or 0)
    candidate_with_ingest = int(candidate_coverage["with_ingest"] or 0)
    legacy_candidate_events = int(candidate_coverage["legacy"] or 0)
    ingest_pct = (
        round(100.0 * candidate_with_ingest / candidate_total, 1) if candidate_total else 0.0
    )
    if candidate_total and ingest_pct < 95.0:
        warnings.append(
            f"server ingest identity covers only {ingest_pct}% of candidate events; "
            f"{legacy_candidate_events} legacy correlation-only event(s) remain"
        )

    return {
        "telemetry_enabled": settings.usage_telemetry_enabled,
        "first_event_at": first_last["first_ts"],
        "last_event_at": first_last["last_ts"],
        "provider_calls_total": total_calls,
        "provider_actual_calls": actual_calls,
        "provider_disabled_calls": disabled_calls,
        "provider_non_attempted_failures": provider_coverage["non_attempted_failures"] or 0,
        "pct_provider_calls_with_tokens": pct_tokens,
        "pct_provider_calls_with_cost": pct_cost,
        "pct_candidate_events_with_ingest_id": ingest_pct,
        "legacy_candidate_event_count": legacy_candidate_events,
        "active_principals": active_principals,
        "active_principals_with_lifecycle_summary": principals_with_summary,
        "active_principals_without_lifecycle_summary": principals_without_summary,
        "warnings": warnings,
    }


async def _candidate_funnel_section(session: AsyncSession, window: ReportWindow) -> dict[str, Any]:
    clause, params = _tenant_clause(window)

    lifecycle_row = (
        await session.execute(
            text(
                "SELECT "
                "COALESCE(sum(input_count), 0) AS extracted, "
                "COALESCE(sum((metadata->>'guard_rejected')::bigint), 0) AS guard_rejected, "
                "COALESCE(sum((metadata->>'classified')::bigint), 0) AS classified, "
                "COALESCE(sum((metadata->>'parked')::bigint), 0) AS parked "
                "FROM usage_events "
                f"WHERE {clause} AND event_type = 'client.lifecycle_summary'"
            ),
            params,
        )
    ).mappings().one()

    candidate_observations = (
        await _scalar(
            session,
            f"SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'candidate.observed'",
            params,
        )
        or 0
    )
    kib_units = (
        await _scalar(
            session,
            "SELECT COALESCE(sum(ceil(input_bytes / 1024.0)), 0) FROM usage_events "
            f"WHERE {clause} AND event_type = 'candidate.observed'",
            params,
        )
        or 0
    )
    byte_pcts = (
        await session.execute(
            text(
                "SELECT "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY input_bytes) AS p50, "
                "percentile_cont(0.9) WITHIN GROUP (ORDER BY input_bytes) AS p90, "
                "percentile_cont(0.99) WITHIN GROUP (ORDER BY input_bytes) AS p99 "
                "FROM usage_events "
                f"WHERE {clause} AND event_type = 'candidate.observed'"
            ),
            params,
        )
    ).mappings().one()

    # candidate.outcome is now append-only PER ATTEMPT (one row per
    # /v1/remember invocation, no dedupe_key — see ENG-METER-001). So the
    # outcome counts below are two views:
    #   * logical_outcomes: one status per correlation_id, resolved as the
    #     earliest non-'failed' attempt (a failed attempt followed by a
    #     successful retry resolves to that success), or 'failed' when no
    #     attempt succeeded. This is what drives the failure/create funnel.
    #   * attempt_*: raw attempt-level counters, including every failed retry.
    cohort_params = _cohort_params(window)
    logical_outcomes = await _rows(
        session,
        f"WITH {_candidate_cohort_cte()} "
        "SELECT status, count(*) AS n FROM logical_candidate_outcomes GROUP BY status",
        cohort_params,
    )
    logical = {r["status"]: r["n"] for r in logical_outcomes}
    distinct_candidates = sum(logical.values())

    attempt_rows = await _rows(
        session,
        "SELECT status, count(*) AS n FROM usage_events "
        f"WHERE {clause} AND event_type = 'candidate.outcome' GROUP BY status",
        params,
    )
    attempts = {r["status"]: r["n"] for r in attempt_rows}
    total_attempts = sum(attempts.values())
    failed_attempts = int(attempts.get("failed", 0))
    successful_attempts = total_attempts - failed_attempts

    cohort_size = int(
        await _scalar(
            session,
            f"WITH {_candidate_cohort_cte()} SELECT count(*) FROM candidate_cohort",
            cohort_params,
        )
        or 0
    )
    unresolved = max(0, cohort_size - distinct_candidates)
    ingest_clause, ingest_params = _tenant_clause(window, alias="ci")
    ingest_counts = (
        await session.execute(
            text(
                "SELECT count(*) AS total, count(*) FILTER (WHERE EXISTS ("
                "SELECT 1 FROM usage_events ue WHERE ue.tenant_id = ci.tenant_id "
                "AND ue.ingest_id = ci.id AND ue.event_type = 'candidate.outcome' "
                "AND ue.created_at < :until)) AS with_outcomes "
                "FROM candidate_ingests ci WHERE " + ingest_clause
            ),
            ingest_params,
        )
    ).mappings().one()
    candidate_ingests = int(ingest_counts["total"] or 0)
    candidate_ingests_with_outcomes = int(ingest_counts["with_outcomes"] or 0)
    legacy_correlation_candidates = int(
        await _scalar(
            session,
            f"WITH {_candidate_cohort_cte()} SELECT count(*) FROM candidate_cohort "
            "WHERE candidate_key LIKE 'legacy:%'",
            cohort_params,
        )
        or 0
    )
    total_identity_candidates = candidate_ingests + legacy_correlation_candidates
    ingest_identity_coverage_pct = (
        round(100.0 * candidate_ingests / total_identity_candidates, 1)
        if total_identity_candidates
        else 0.0
    )
    cohort_attempts = int(
        await _scalar(
            session,
            f"WITH {_candidate_cohort_cte()} SELECT count(*) FROM usage_events ue "
            "JOIN candidate_cohort cc ON cc.tenant_id = ue.tenant_id "
            "AND COALESCE('ingest:' || ue.ingest_id::text, "
            "'legacy:' || ue.correlation_id::text) = cc.candidate_key "
            "WHERE ue.event_type = 'candidate.outcome' AND ue.created_at < :until",
            cohort_params,
        )
        or 0
    )
    success_clause, success_params = _tenant_clause(window, alias="success")
    retry_successes = int(
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events success WHERE "
            + success_clause
            + " AND success.event_type = 'candidate.outcome' AND success.status != 'failed' "
            "AND NOT EXISTS (SELECT 1 FROM usage_events earlier_success WHERE "
            "earlier_success.tenant_id = success.tenant_id "
            "AND COALESCE('ingest:' || earlier_success.ingest_id::text, "
            "'legacy:' || earlier_success.correlation_id::text) = "
            "COALESCE('ingest:' || success.ingest_id::text, "
            "'legacy:' || success.correlation_id::text) "
            "AND earlier_success.event_type = 'candidate.outcome' "
            "AND earlier_success.status != 'failed' "
            "AND (earlier_success.created_at, earlier_success.id) < "
            "(success.created_at, success.id)) "
            "AND EXISTS (SELECT 1 FROM usage_events failure WHERE "
            "failure.tenant_id = success.tenant_id "
            "AND COALESCE('ingest:' || failure.ingest_id::text, "
            "'legacy:' || failure.correlation_id::text) = "
            "COALESCE('ingest:' || success.ingest_id::text, "
            "'legacy:' || success.correlation_id::text) "
            "AND failure.event_type = 'candidate.outcome' AND failure.status = 'failed' "
            "AND (failure.created_at, failure.id) < (success.created_at, success.id))",
            success_params,
        )
        or 0
    )
    success_latency = (
        await session.execute(
            text(
                f"WITH {_candidate_cohort_cte()} SELECT "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "EXTRACT(EPOCH FROM (created_at - first_seen_at)) * 1000) AS p50, "
                "percentile_cont(0.9) WITHIN GROUP (ORDER BY "
                "EXTRACT(EPOCH FROM (created_at - first_seen_at)) * 1000) AS p90, "
                "percentile_cont(0.99) WITHIN GROUP (ORDER BY "
                "EXTRACT(EPOCH FROM (created_at - first_seen_at)) * 1000) AS p99 "
                "FROM logical_candidate_outcomes WHERE status != 'failed'"
            ),
            cohort_params,
        )
    ).mappings().one()
    new_memory_writes = int(logical.get("created", 0)) + int(logical.get("superseded", 0))

    return {
        "lifecycle_extracted": int(lifecycle_row["extracted"]),
        "lifecycle_guard_rejected": int(lifecycle_row["guard_rejected"]),
        "lifecycle_classified": int(lifecycle_row["classified"]),
        "lifecycle_parked": int(lifecycle_row["parked"]),
        "candidate_observations": int(candidate_observations),
        "candidate_cohort_size": cohort_size,
        "candidate_ingests": candidate_ingests,
        "candidate_ingests_with_outcomes": candidate_ingests_with_outcomes,
        "candidate_ingests_unresolved": max(
            0, candidate_ingests - candidate_ingests_with_outcomes
        ),
        "legacy_correlation_candidates": legacy_correlation_candidates,
        "ingest_identity_coverage_pct": ingest_identity_coverage_pct,
        # Logical-outcome funnel (one outcome per correlation_id).
        "logical_candidates": int(distinct_candidates),
        "unresolved_candidates": unresolved,
        "remember_attempts": int(total_attempts),
        "created": int(logical.get("created", 0)),
        "deduped": int(logical.get("deduped", 0)),
        "superseded": int(logical.get("superseded", 0)),
        "failed": int(logical.get("failed", 0)),
        "new_memory_writes": new_memory_writes,
        # Attempt-level diagnostics (every /v1/remember invocation).
        "total_attempts": int(total_attempts),
        "distinct_candidates": int(distinct_candidates),
        "failed_attempts": failed_attempts,
        "successful_attempts": successful_attempts,
        "retry_successes_in_window": retry_successes,
        "attempts_per_cohort_candidate_avg": (
            round(cohort_attempts / cohort_size, 2) if cohort_size else 0.0
        ),
        "attempts_per_candidate_avg": (
            round(cohort_attempts / cohort_size, 2) if cohort_size else 0.0
        ),
        "time_to_first_success_ms_p50": success_latency["p50"],
        "time_to_first_success_ms_p90": success_latency["p90"],
        "time_to_first_success_ms_p99": success_latency["p99"],
        "flat_candidate_units": int(candidate_observations),
        "kib_candidate_units": int(kib_units),
        "candidate_bytes_p50": byte_pcts["p50"],
        "candidate_bytes_p90": byte_pcts["p90"],
        "candidate_bytes_p99": byte_pcts["p99"],
    }


async def _source_type_section(session: AsyncSession, window: ReportWindow) -> list[dict[str, Any]]:
    clause, params = _tenant_clause(window)
    return await _rows(
        session,
        "SELECT COALESCE(source_type, 'unknown') AS source_type, "
        "count(*) AS candidate_observations, "
        "COALESCE(sum(input_bytes), 0) AS candidate_bytes, "
        "COALESCE(sum(ceil(input_bytes / 1024.0)), 0) AS kib_candidate_units "
        "FROM usage_events "
        f"WHERE {clause} AND event_type = 'candidate.observed' "
        "GROUP BY source_type ORDER BY candidate_observations DESC",
        params,
    )


async def _principal_section(session: AsyncSession, window: ReportWindow) -> list[dict[str, Any]]:
    clause, params = _tenant_clause(window, alias="ue")
    sql = f"""
        WITH {_candidate_cohort_cte()},
        cand AS (
            SELECT principal_id, count(*) AS candidate_count,
                   COALESCE(sum(ceil(input_bytes / 1024.0)), 0) AS kib_units
            FROM usage_events ue
            WHERE {clause} AND event_type = 'candidate.observed'
            GROUP BY principal_id
        ),
        writes AS (
            SELECT principal_id,
                   count(*) FILTER (WHERE status = 'created') AS created_count,
                   count(*) FILTER (WHERE status IN ('created', 'superseded'))
                       AS new_memory_write_count
            FROM logical_candidate_outcomes ue
            GROUP BY principal_id
        ),
        retrieval AS (
            SELECT principal_id, count(*) AS retrieval_count
            FROM usage_events ue
            WHERE {clause} AND event_type = 'retrieval.request'
            GROUP BY principal_id
        ),
        tokens AS (
            SELECT principal_id, COALESCE(sum(total_tokens), 0) AS provider_tokens
            FROM usage_events ue
            WHERE {clause} AND event_type = 'provider.call'
            GROUP BY principal_id
        )
        SELECT
            COALESCE(cand.principal_id, writes.principal_id, retrieval.principal_id,
                     tokens.principal_id) AS principal_id,
            p.name AS principal_name,
            p.type AS principal_type,
            COALESCE(cand.candidate_count, 0) AS candidate_count,
            COALESCE(cand.kib_units, 0) AS kib_candidate_units,
            COALESCE(writes.created_count, 0) AS created_count,
            COALESCE(writes.new_memory_write_count, 0) AS new_memory_write_count,
            COALESCE(retrieval.retrieval_count, 0) AS retrieval_count,
            COALESCE(tokens.provider_tokens, 0) AS provider_tokens
        FROM cand
        FULL OUTER JOIN writes ON writes.principal_id = cand.principal_id
        FULL OUTER JOIN retrieval
            ON retrieval.principal_id = COALESCE(cand.principal_id, writes.principal_id)
        FULL OUTER JOIN tokens
            ON tokens.principal_id = COALESCE(
                cand.principal_id, writes.principal_id, retrieval.principal_id
            )
        LEFT JOIN principals p ON p.id = COALESCE(cand.principal_id, writes.principal_id,
                                                   retrieval.principal_id, tokens.principal_id)
        ORDER BY candidate_count DESC
    """
    return await _rows(session, sql, {**params, "tenant_id": window.tenant_id})


async def _provider_economics_section(
    session: AsyncSession, window: ReportWindow
) -> list[dict[str, Any]]:
    clause, params = _tenant_clause(window)
    sql = f"""
        SELECT
            COALESCE(usage_class, 'unknown') AS usage_class,
            operation,
            provider_host,
            model,
            count(*) AS calls,
            count(*) FILTER (WHERE external_call_attempted IS TRUE) AS actual_calls,
            count(*) FILTER (WHERE status = 'failed' AND external_call_attempted IS FALSE)
                AS non_attempted_failures,
            count(*) FILTER (WHERE status = 'succeeded') AS successes,
            count(*) FILTER (WHERE status = 'failed') AS failures,
            count(*) FILTER (WHERE status = 'disabled') AS disabled_n,
            count(*) FILTER (WHERE metadata->>'application_fallback' = 'true')
                AS application_fallbacks,
            COALESCE(sum(input_count), 0) AS input_count,
            COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(sum(completion_tokens), 0) AS completion_tokens,
            COALESCE(sum(total_tokens), 0) AS total_tokens,
            sum(reported_cost_usd) AS reported_cost_usd,
            count(*) FILTER (
                WHERE external_call_attempted IS TRUE AND reported_cost_usd IS NOT NULL
            ) AS with_reported_cost,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS latency_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS latency_p90,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS latency_p99
        FROM usage_events
        WHERE {clause} AND event_type = 'provider.call'
        GROUP BY usage_class, operation, provider_host, model
        ORDER BY calls DESC
    """
    rows = await _rows(session, sql, params)
    for row in rows:
        # ``calls`` is retained as the compatibility all-operation count;
        # ``actual_calls`` is the external-call count used for coverage.
        # reported-cost coverage excludes disabled rows (no external call, so
        # no provider cost to report).
        actual = row["actual_calls"] or 0
        row["reported_cost_coverage_pct"] = (
            round(100.0 * row["with_reported_cost"] / actual, 1) if actual else 0.0
        )
    return rows


async def _provider_totals_section(
    session: AsyncSession, window: ReportWindow
) -> dict[str, Any]:
    clause, params = _tenant_clause(window)
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS all_provider_operations, "
                "count(*) FILTER (WHERE external_call_attempted IS TRUE) "
                "AS all_actual_provider_calls, "
                "count(*) FILTER (WHERE status = 'failed' "
                "AND external_call_attempted IS FALSE) AS all_non_attempted_failures, "
                "count(*) FILTER (WHERE status = 'disabled') AS all_disabled_operations, "
                "COALESCE(sum(total_tokens), 0) AS all_provider_tokens, "
                "COALESCE(sum(reported_cost_usd), 0) AS all_reported_cost_usd, "
                "count(*) FILTER (WHERE usage_class IN ('request', 'async_enrichment')) "
                "AS product_provider_operations, "
                "count(*) FILTER (WHERE usage_class IN ('request', 'async_enrichment') "
                "AND external_call_attempted IS TRUE) AS product_actual_provider_calls, "
                "COALESCE(sum(total_tokens) FILTER (WHERE usage_class IN "
                "('request', 'async_enrichment')), 0) AS product_provider_tokens, "
                "COALESCE(sum(reported_cost_usd) FILTER (WHERE usage_class IN "
                "('request', 'async_enrichment')), 0) AS product_reported_cost_usd, "
                "count(*) FILTER (WHERE usage_class = 'maintenance') "
                "AS maintenance_provider_operations, "
                "count(*) FILTER (WHERE usage_class = 'maintenance' "
                "AND external_call_attempted IS TRUE) AS maintenance_actual_provider_calls, "
                "COALESCE(sum(total_tokens) FILTER (WHERE usage_class = 'maintenance'), 0) "
                "AS maintenance_provider_tokens, "
                "COALESCE(sum(reported_cost_usd) FILTER (WHERE usage_class = 'maintenance'), 0) "
                "AS maintenance_reported_cost_usd, "
                "count(*) FILTER (WHERE usage_class = 'diagnostic') "
                "AS diagnostic_provider_operations, "
                "count(*) FILTER (WHERE usage_class = 'diagnostic' "
                "AND external_call_attempted IS TRUE) AS diagnostic_actual_provider_calls, "
                "COALESCE(sum(total_tokens) FILTER (WHERE usage_class = 'diagnostic'), 0) "
                "AS diagnostic_provider_tokens, "
                "COALESCE(sum(reported_cost_usd) FILTER (WHERE usage_class = 'diagnostic'), 0) "
                "AS diagnostic_reported_cost_usd "
                f"FROM usage_events WHERE {clause} AND event_type = 'provider.call'"
            ),
            params,
        )
    ).mappings().one()
    return dict(row)


async def _conflict_economics_section(
    session: AsyncSession, window: ReportWindow
) -> dict[str, Any]:
    clause, params = _tenant_clause(window)
    conflict_calls = (
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'provider.call' "
            "AND operation = 'conflict_classification'",
            params,
        )
        or 0
    )
    # ``disabled`` conflict operations are not external LLM calls (the provider
    # is ``none``); exclude them from the actual-call count so inference volume
    # is not overstated when a provider is disabled.
    conflict_actual_calls = (
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'provider.call' "
            "AND operation = 'conflict_classification' AND external_call_attempted IS TRUE",
            params,
        )
        or 0
    )
    candidate_observations = (
        await _scalar(
            session,
            f"SELECT count(*) FROM usage_events WHERE {clause} "
            "AND event_type = 'candidate.observed'",
            params,
        )
        or 0
    )
    per_1000 = (
        round(1000.0 * conflict_actual_calls / candidate_observations, 2)
        if candidate_observations
        else 0.0
    )
    verdicts = await _rows(
        session,
        "SELECT metadata->>'verdict' AS verdict, count(*) AS n FROM usage_events "
        f"WHERE {clause} AND event_type = 'provider.call' "
        "AND operation = 'conflict_classification' AND metadata->>'verdict' IS NOT NULL "
        "GROUP BY metadata->>'verdict'",
        params,
    )
    failed_calls = (
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'provider.call' "
            "AND operation = 'conflict_classification' AND status = 'failed'",
            params,
        )
        or 0
    )
    application_fallbacks = (
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'provider.call' "
            "AND operation = 'conflict_classification' "
            "AND metadata->>'application_fallback' = 'true'",
            params,
        )
        or 0
    )
    return {
        # Total conflict_classification provider rows (includes disabled).
        "conflict_classifications": int(conflict_calls),
        # Actual external LLM calls (excludes disabled, which never called a
        # provider). The per-1000 ratio uses this so it is not inflated when a
        # provider is disabled.
        "conflict_actual_calls": int(conflict_actual_calls),
        "conflict_calls_per_1000_candidate_observations": per_1000,
        "verdict_distribution": {r["verdict"]: r["n"] for r in verdicts},
        # A failed provider call and an application fallback are the same event
        # now (fallback is metadata on a 'failed' row), so the legacy
        # "failed_or_fallback_count" is the failed count. Kept under the old
        # key for backward compat; failed_calls / application_fallbacks are the
        # precise new keys.
        "failed_or_fallback_count": int(failed_calls),
        "failed_calls": int(failed_calls),
        "application_fallback_count": int(application_fallbacks),
    }


async def _retrieval_section(session: AsyncSession, window: ReportWindow) -> dict[str, Any]:
    clause, params = _tenant_clause(window)
    by_mode = await _rows(
        session,
        "SELECT operation, count(*) AS requests, "
        "COALESCE(sum(input_count), 0) AS item_total, "
        "COALESCE(sum(input_bytes), 0) AS byte_total "
        "FROM usage_events "
        f"WHERE {clause} AND event_type = 'retrieval.request' "
        "GROUP BY operation ORDER BY requests DESC",
        params,
    )
    query_embeddings = (
        await session.execute(
            text(
                "SELECT "
                "count(*) AS calls, "
                "count(*) FILTER (WHERE external_call_attempted IS TRUE) AS actual_calls, "
                "COALESCE(sum(total_tokens), 0) AS tokens "
                "FROM usage_events WHERE "
                + clause
                + " AND event_type = 'provider.call' "
                "AND operation IN ('embedding_query_recall', 'embedding_query_search')"
            ),
            params,
        )
    ).mappings().one()

    semantic_query_ops = ("semantic_recall", "semantic_search", "hybrid_search")
    semantic_queries = (
        await _scalar(
            session,
            "SELECT count(*) FROM usage_events "
            f"WHERE {clause} AND event_type = 'retrieval.request' "
            "AND operation = ANY(:ops)",
            {**params, "ops": list(semantic_query_ops)},
        )
        or 0
    )
    new_memory_write_count = (
        await _scalar(
            session,
            f"WITH {_candidate_cohort_cte()} "
            "SELECT count(*) FROM logical_candidate_outcomes "
            "WHERE status IN ('created', 'superseded')",
            _cohort_params(window),
        )
        or 0
    )
    active_principals = (
        await _scalar(
            session,
            f"SELECT count(DISTINCT principal_id) FROM usage_events WHERE {clause} "
            "AND principal_id IS NOT NULL",
            params,
        )
        or 0
    )
    total_retrieval = sum(r["requests"] for r in by_mode)
    return {
        "by_mode": by_mode,
        "total_requests": total_retrieval,
        # Total query-embedding provider rows (includes disabled).
        "query_embedding_calls": int(query_embeddings["calls"]),
        # Actual external embedding calls (excludes disabled).
        "query_embedding_actual_calls": int(query_embeddings["actual_calls"]),
        "query_embedding_tokens": int(query_embeddings["tokens"]),
        "semantic_queries_per_new_memory_write": (
            round(semantic_queries / new_memory_write_count, 2)
            if new_memory_write_count
            else 0.0
        ),
        "semantic_queries_per_created_memory": (
            round(semantic_queries / new_memory_write_count, 2)
            if new_memory_write_count
            else 0.0
        ),
        "retrievals_per_active_principal": (
            round(total_retrieval / active_principals, 2) if active_principals else 0.0
        ),
    }


async def _worker_section(session: AsyncSession, window: ReportWindow) -> dict[str, Any]:
    tenant_filter = ""
    params: dict[str, Any] = {}
    if window.tenant_id is not None:
        tenant_filter = " AND tenant_id = :tenant_id"
        params["tenant_id"] = window.tenant_id

    by_type_status = await _rows(
        session,
        "SELECT job_type, status, count(*) AS n FROM jobs "
        f"WHERE 1=1{tenant_filter} GROUP BY job_type, status ORDER BY job_type, status",
        params,
    )
    oldest_pending_seconds = await _scalar(
        session,
        "SELECT EXTRACT(EPOCH FROM (now() - min(created_at))) FROM jobs "
        f"WHERE status = 'pending'{tenant_filter}",
        params,
    )
    attempts_dist = await _rows(
        session,
        f"SELECT attempts, count(*) AS n FROM jobs WHERE 1=1{tenant_filter} "
        "GROUP BY attempts ORDER BY attempts",
        params,
    )
    _lat_expr = "EXTRACT(EPOCH FROM (completed_at - created_at))"
    latency_pcts = (
        await session.execute(
            text(
                "SELECT "
                f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {_lat_expr}) AS p50, "
                f"percentile_cont(0.9) WITHIN GROUP (ORDER BY {_lat_expr}) AS p90, "
                f"percentile_cont(0.99) WITHIN GROUP (ORDER BY {_lat_expr}) AS p99 "
                "FROM jobs WHERE status = 'succeeded' AND completed_at IS NOT NULL"
                f"{tenant_filter}"
            ),
            params,
        )
    ).mappings().one()
    return {
        "by_job_type_status": by_type_status,
        "oldest_pending_age_seconds": oldest_pending_seconds,
        "attempts_distribution": attempts_dist,
        "completion_latency_seconds_p50": latency_pcts["p50"],
        "completion_latency_seconds_p90": latency_pcts["p90"],
        "completion_latency_seconds_p99": latency_pcts["p99"],
    }


async def _storage_section(session: AsyncSession, window: ReportWindow) -> dict[str, Any]:
    tenant_scoped = window.tenant_id is not None
    tenant_filter_mi = ""
    params: dict[str, Any] = {}
    if tenant_scoped:
        tenant_filter_mi = " AND tenant_id = :tenant_id"
        params["tenant_id"] = window.tenant_id

    totals = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE valid_to IS NULL) AS live, "
                "count(*) FILTER (WHERE review_status = 'active' "
                "AND valid_to IS NULL) AS active_n, "
                "count(*) FILTER (WHERE review_status = 'proposed' "
                "AND valid_to IS NULL) AS proposed_n, "
                "count(*) FILTER (WHERE review_status = 'disputed' "
                "AND valid_to IS NULL) AS disputed_n, "
                # review_status='archived' is the archived population (NOT
                # valid_to IS NOT NULL, which also covers superseded + manually
                # invalidated memories — see ENG-METER-001 blocking correction).
                "count(*) FILTER (WHERE review_status = 'archived') AS archived_n, "
                "count(*) FILTER (WHERE review_status = 'rejected') AS rejected_n, "
                # Non-current rows that still occupy durable storage but are NOT
                # archived and NOT superseded — manually invalidated memories
                # only (superseded rows are counted separately below).
                "count(*) FILTER (WHERE valid_to IS NOT NULL "
                "AND review_status NOT IN ('archived', 'rejected') "
                "AND superseded_by IS NULL) AS invalidated_n, "
                "count(*) FILTER (WHERE superseded_by IS NOT NULL) AS superseded_n "
                f"FROM memory_items WHERE 1=1{tenant_filter_mi}"
            ),
            params,
        )
    ).mappings().one()

    embeddings = (
        await session.execute(
            text(
                "SELECT "
                "count(*) FILTER (WHERE embedding_status = 'ready') AS ready, "
                "count(*) FILTER (WHERE embedding_status = 'pending') AS pending, "
                "count(*) FILTER (WHERE embedding_status = 'failed') AS failed "
                f"FROM memory_embeddings WHERE 1=1{tenant_filter_mi}"
            ),
            params,
        )
    ).mappings().one()

    profile_counts = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE state IN ('active', 'candidate')) AS writable "
                "FROM embedding_profiles"
            )
        )
    ).mappings().one()

    tables = (
        "memory_items", "memory_embeddings", "memory_edges", "kg_triples",
        "item_events", "recall_logs", "jobs", "usage_events", "candidate_ingests",
    )
    table_sizes: dict[str, int | None] = {}
    index_sizes: dict[str, int | None] = {}
    for tbl in tables:
        table_sizes[tbl] = await _scalar(
            session, "SELECT pg_total_relation_size(:t)", {"t": tbl}
        )
        index_sizes[tbl] = await _scalar(
            session, "SELECT pg_indexes_size(:t)", {"t": tbl}
        )

    db_size = await _scalar(session, "SELECT pg_database_size(current_database())", {})

    # bytes_per_retained_memory divides the GLOBAL physical relation size by the
    # retained row count. Superseded/rejected/invalidated/archived rows still
    # occupy storage, so the denominator is ALL rows (``total``), not just live
    # — using live alone overstated per-memory cost (ENG-METER-001 correction).
    total_rows = totals["total"] or 0
    live = totals["live"] or 0
    ready = embeddings["ready"] or 0

    # Under --tenant the counts are tenant-filtered but pg_total_relation_size
    # is GLOBAL, so a per-memory physical ratio would be meaningless (a small
    # tenant would appear to consume the whole deployment's table). Suppress it
    # and provide a clearly-labeled LOGICAL estimate instead.
    bytes_per_memory: float | None = None
    bytes_per_memory_note = (
        "suppressed: pg_total_relation_size is global and cannot be attributed "
        "to one tenant; see logical_tenant_bytes for a logical estimate"
        if tenant_scoped
        else None
    )
    if not tenant_scoped and total_rows and table_sizes["memory_items"] is not None:
        bytes_per_memory = round(table_sizes["memory_items"] / total_rows, 1)

    bytes_per_embedding: float | None = None
    if not tenant_scoped and ready and table_sizes["memory_embeddings"] is not None:
        bytes_per_embedding = round(table_sizes["memory_embeddings"] / ready, 1)

    # Logical tenant-bytes estimate: the tenant's share of the global table
    # physical size, proportional to its row count. Clearly an estimate, never
    # a physical measurement — and only meaningful when tenant-scoped (a
    # deployment-wide report would just echo the global sizes above).
    logical_tenant_bytes: dict[str, int | None] | None = None
    if tenant_scoped:
        global_items = await _scalar(
            session, "SELECT count(*) FROM memory_items", {}
        )
        logical_tenant_bytes = {}
        if global_items and table_sizes["memory_items"] is not None:
            logical_tenant_bytes["memory_items"] = round(
                table_sizes["memory_items"] * total_rows / global_items
            )
        else:
            logical_tenant_bytes["memory_items"] = None

    warnings: list[str] = []
    if tenant_scoped:
        warnings.append(
            "tenant-scoped report: bytes_per_retained_memory is suppressed and "
            "global_physical_bytes is deployment-wide (not attributable to this "
            "tenant); logical_tenant_bytes is a proportional estimate only."
        )

    return {
        "memory_items_total": totals["total"],
        "memory_items_live": live,
        "memory_items_active": totals["active_n"],
        "memory_items_proposed": totals["proposed_n"],
        "memory_items_disputed": totals["disputed_n"],
        "memory_items_rejected": totals["rejected_n"],
        "memory_items_archived": totals["archived_n"],
        "memory_items_invalidated": totals["invalidated_n"],
        "memory_items_superseded": totals["superseded_n"],
        "embeddings_ready": ready,
        "embeddings_pending": embeddings["pending"],
        "embeddings_failed": embeddings["failed"],
        "embedding_profiles_total": profile_counts["total"],
        "embedding_profiles_writable": profile_counts["writable"],
        # Global physical sizes — deployment-wide, never tenant-attributeable.
        "global_physical_bytes": {
            "table_bytes": table_sizes,
            "index_bytes": index_sizes,
            "database_bytes": db_size,
        },
        # Backward-compatible aliases (deployment-wide physical sizes).
        "table_bytes": table_sizes,
        "index_bytes": index_sizes,
        "database_bytes": db_size,
        "logical_tenant_bytes": logical_tenant_bytes,
        "bytes_per_retained_memory": bytes_per_memory,
        "bytes_per_retained_memory_note": bytes_per_memory_note,
        "bytes_per_ready_embedding": bytes_per_embedding,
        "warnings": warnings,
    }


async def _hourly_series(session: AsyncSession, window: ReportWindow) -> list[dict[str, Any]]:
    clause, params = _tenant_clause(window)
    sql = f"""
        SELECT
            date_trunc('hour', created_at) AS hour,
            count(*) FILTER (WHERE event_type = 'candidate.observed') AS candidate_observations,
            COALESCE(sum(ceil(input_bytes / 1024.0)) FILTER (
                WHERE event_type = 'candidate.observed'), 0) AS kib_candidate_units,
            count(*) FILTER (WHERE event_type = 'provider.call') AS provider_calls,
            count(*) FILTER (WHERE event_type = 'provider.call') AS provider_operations,
            count(*) FILTER (WHERE event_type = 'provider.call'
                AND external_call_attempted IS TRUE)
                AS actual_provider_calls,
            count(*) FILTER (WHERE event_type = 'provider.call'
                AND usage_class IN ('request', 'async_enrichment')
                AND external_call_attempted IS TRUE) AS product_actual_provider_calls,
            count(*) FILTER (WHERE event_type = 'provider.call'
                AND usage_class = 'maintenance'
                AND external_call_attempted IS TRUE) AS maintenance_actual_provider_calls,
            count(*) FILTER (WHERE event_type = 'provider.call'
                AND usage_class = 'diagnostic'
                AND external_call_attempted IS TRUE) AS diagnostic_actual_provider_calls,
            COALESCE(sum(total_tokens) FILTER (WHERE event_type = 'provider.call'
                AND usage_class IN ('request', 'async_enrichment')), 0) AS product_tokens,
            COALESCE(sum(reported_cost_usd) FILTER (WHERE event_type = 'provider.call'
                AND usage_class IN ('request', 'async_enrichment')), 0)
                AS product_reported_cost_usd,
            COALESCE(sum(total_tokens)
                FILTER (WHERE event_type = 'provider.call'), 0) AS total_tokens,
            sum(reported_cost_usd) FILTER (WHERE event_type = 'provider.call') AS reported_cost_usd,
            count(*) FILTER (WHERE event_type = 'retrieval.request') AS retrieval_requests,
            count(*) FILTER (WHERE status = 'failed') AS failures
        FROM usage_events
        WHERE {clause}
        GROUP BY hour
        ORDER BY hour
    """
    return await _rows(session, sql, params)


async def build_report(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Build the full dogfood usage report as a JSON-serializable dict.

    ``session`` must be connected as a role that can see every tenant's rows
    when ``tenant_id`` is ``None`` (the owner/migration role bypasses RLS —
    see ``engram.db.owner_session_factory``); when ``tenant_id`` is given,
    every query filters explicitly so results are correct under RLS too.
    """
    window = default_window(tenant=tenant_id, since=since, until=until)
    provider_totals = await _provider_totals_section(session, window)
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "since": window.since.isoformat(),
        "until": window.until.isoformat(),
        "coverage": await _coverage_section(session, window),
        "candidate_funnel": await _candidate_funnel_section(session, window),
        "by_source_type": await _source_type_section(session, window),
        "by_principal": await _principal_section(session, window),
        "provider_economics": await _provider_economics_section(session, window),
        **provider_totals,
        "conflict_economics": await _conflict_economics_section(session, window),
        "retrieval": await _retrieval_section(session, window),
        "worker": await _worker_section(session, window),
        "storage": await _storage_section(session, window),
        "hourly_series": await _hourly_series(session, window),
    }
