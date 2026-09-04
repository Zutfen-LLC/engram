"""Bounded promotion reconciliation backstop (ENG-PROMOTION-003B4 / issue #155).

The backstop is **orchestration**, not a second promotion implementation: it
discovers live proposals whose targeted evaluation work is missing or dead and
enqueues canonical ``promotion.evaluate`` jobs (or, for provider recovery,
re-enqueues the existing async ``classification.refine`` contract). It never
decides or performs ``proposed -> active`` — lifecycle authority remains
``promotion.evaluate`` → reload current state → shared evaluator →
promotion-time conflict recheck → guarded mutation/audit. There is no second
promotion formula here: every decision about what an item needs is derived
from the same shared evaluator (``engram.promotion.assess_promotion_candidate``)
the mutation paths use.

Topology (no separate scheduler daemon — everything runs through the existing
PostgreSQL job queue):

* ``promotion.reconcile`` is a tenant-scoped canonical job whose payload is a
  versioned, exact/closed, identifier/provenance-only envelope
  (``promotion-reconcile-v1``): ``reason`` (closed vocabulary), stable
  ``trigger_id``, and the centrally computed ``dedupe_key``. No thresholds,
  scores, decision state, credentials, or content ever enter the payload, and
  there is no metadata escape hatch — unknown fields/reasons/versions fail
  closed through ordinary retry/dead-letter behavior.
* The **backstop chain** (``reason='backstop'``): each pass enqueues the next
  pass at ``now + promotion_reconciliation_interval_seconds`` under a fixed
  per-tenant dedupe key, so at most one pending backstop pass per tenant
  exists and the queue stays globally fair across tenants. The worker loop
  bootstraps/heals chains (``ensure_periodic_reconciliation_chains``) through
  a durable owner-only tenant keyset cursor — bounded, content-free
  bookkeeping only, never an all-tenant materialization.
* **Request chains** (``policy_change`` / ``provider_recovery`` /
  ``operator_request``): a committed policy change or an explicit authorized
  request resets the tenant's rotation cursor and enqueues one bounded pass;
  the chain self-continues (immediately due, same dedupe identity) until the
  rotation reaches the tail, then stops. Per-pass work stays bounded and the
  durable continuation survives crashes.

Candidate discovery is a bounded keyset rotation over live proposals
(``review_status='proposed' AND valid_to IS NULL AND superseded_by IS NULL``,
kind-policy-eligible only, ``ORDER BY (created_at, id) LIMIT n`` strictly
after the persisted cursor in ``promotion_reconcile_state``), served by the
partial index ``idx_memitems_proposed_rotation``. Terminal-under-current-policy
rows get a scheduler-only suppression marker for the current cursor epoch and
are excluded from later ordinary periodic selection. Relevant item/evidence
events invalidate the marker; policy/operator/provider resets advance the
epoch. No promotion decision state is persisted.

Restart/crash safety: repairs, cursor advance, and chain continuation commit
in ONE transaction — a crash before commit replays safely (repairs are
idempotent through the canonical promotion.evaluate dedupe identity), a crash
after commit leaves a healthy pending continuation. Stale concurrent passes
cannot create permanent holes: the cursor advance is guarded by
``cursor_epoch`` (bumped on every reset), so a pre-reset pass no-ops instead
of overwriting the post-reset position, and an ordinary last-writer-wins
regression only widens the next pass's keyset window.

Startup recall's lazy promotion pass (``maybe_auto_promote_for_startup_
recall``) is deliberately untouched in this slice; removing its mutation is
B5's shadow-parity cutover.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import (
    Job,
    MemoryItem,
    PromotionReconcileSchedulerState,
    PromotionReconcileState,
    PromotionReconcileTerminal,
    Tenant,
)
from engram.promotion import (
    TRIGGER_POLICY_CHANGED,
    TRIGGER_RECONCILE,
    _config,
    _config_values,
    _kind_promotion_allowed,
    assess_promotion_candidate,
    enqueue_promotion_evaluation,
    load_promotion_support,
)
from engram.promotion_policy import (
    EVIDENCE_PROMOTION_POLICY_VERSION,
    LEGACY_PROMOTION_POLICY_VERSION,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PROMOTION_RECONCILE_ALLOWED_FIELDS",
    "PROMOTION_RECONCILE_CONTRACT_VERSION",
    "PROMOTION_RECONCILE_JOB_TYPE",
    "PROMOTION_RECONCILE_REASONS",
    "PromotionReconcileContractError",
    "PromotionReconcilePayload",
    "RECONCILE_REASON_BACKSTOP",
    "RECONCILE_REASON_OPERATOR_REQUEST",
    "RECONCILE_REASON_POLICY_CHANGE",
    "RECONCILE_REASON_PROVIDER_RECOVERY",
    "ReconciliationPassResult",
    "TenantSchedulingResult",
    "BACKSTOP_TRIGGER_ID",
    "build_promotion_reconcile_payload",
    "bump_kind_policy_revision",
    "ensure_periodic_reconciliation_chains",
    "parse_promotion_reconcile_payload",
    "promotion_reconcile_continuation_key",
    "promotion_reconcile_dedupe_key",
    "reconciliation_status",
    "request_global_reconciliation_window",
    "request_reconciliation_chain",
    "run_reconciliation_pass",
]

# ---------------------------------------------------------------------------
# Job contract: promotion.reconcile / promotion-reconcile-v1
# ---------------------------------------------------------------------------

PROMOTION_RECONCILE_JOB_TYPE = "promotion.reconcile"
PROMOTION_RECONCILE_CONTRACT_VERSION = "promotion-reconcile-v1"

# The closed reason vocabulary — why this reconciliation pass exists. The
# reason selects chain behavior (backstop self-reschedules on the interval;
# request chains continue immediately until the rotation completes) and the
# truthful trigger provenance of the item evaluations the pass enqueues.
RECONCILE_REASON_BACKSTOP = "backstop"
RECONCILE_REASON_POLICY_CHANGE = "policy_change"
RECONCILE_REASON_PROVIDER_RECOVERY = "provider_recovery"
RECONCILE_REASON_OPERATOR_REQUEST = "operator_request"

# Stable trigger identity of the perpetual periodic chain.
BACKSTOP_TRIGGER_ID = "periodic"

PROMOTION_RECONCILE_REASONS: frozenset[str] = frozenset(
    {
        RECONCILE_REASON_BACKSTOP,
        RECONCILE_REASON_POLICY_CHANGE,
        RECONCILE_REASON_PROVIDER_RECOVERY,
        RECONCILE_REASON_OPERATOR_REQUEST,
    }
)
_REQUEST_REASONS: frozenset[str] = frozenset(
    {
        RECONCILE_REASON_POLICY_CHANGE,
        RECONCILE_REASON_PROVIDER_RECOVERY,
        RECONCILE_REASON_OPERATOR_REQUEST,
    }
)

# The exact, closed field set of a promotion-reconcile-v1 payload. Every field
# is identifier/provenance-only; no decision state, content, or credentials
# belong in this contract, and there is deliberately no generic metadata bag.
PROMOTION_RECONCILE_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"contract_version", "reason", "trigger_id", "dedupe_key"}
)

# Diagnostics return only a compact sample of queue history.  Correctness
# lookups do not use this cap: they use one indexed EXISTS probe per item in
# the already-bounded reconciliation window.
_MAX_DIAGNOSTIC_JOB_ROWS = 1000

_CLASSIFICATION_REFINE_JOB_TYPE = "classification.refine"


class PromotionReconcileContractError(ValueError):
    """A ``promotion.reconcile`` payload is malformed, or carries an unknown/
    unsupported contract version or reason.

    Deliberately a ``ValueError`` subclass so it participates in the worker's
    ordinary retry/dead-letter machinery rather than being swallowed as a
    silent no-op.
    """


@dataclass(frozen=True)
class PromotionReconcilePayload:
    """A parsed, validated ``promotion-reconcile-v1`` job payload.

    ``reason`` names why the pass exists. ``trigger_id`` is the stable
    provenance identity of that reason — the periodic backstop's fixed
    ``BACKSTOP_TRIGGER_ID``, a memory-kind policy revision
    (``kind-policy:<revision>``), or the caller-supplied stable identity of a
    provider-recovery / operator request.
    """

    contract_version: str
    reason: str
    trigger_id: str
    dedupe_key: str


def promotion_reconcile_dedupe_key(reason: str, trigger_id: str) -> str:
    """The canonical dedupe key for one reconciliation chain.

    Computed centrally — never accepted verbatim from a caller — and used for
    a chain's FIRST link, so at most one pending/running link-1 job can exist
    for the same ``(tenant_id, reason, trigger_id)`` identity (the jobs
    partial unique index is per tenant). Continuation links carry
    :func:`promotion_reconcile_continuation_key` instead: a chain
    self-continues while its current link is still ``running`` (which the
    partial unique index covers), so a link-suffixed key is what allows the
    next pass to exist at all — the canonical key alone would dedupe the
    continuation into the running job and silently kill the chain.
    """
    return f"{PROMOTION_RECONCILE_JOB_TYPE}:{reason}:{trigger_id}"


def promotion_reconcile_continuation_key(
    reason: str, trigger_id: str, parent_job_id: uuid.UUID | str
) -> str:
    """The dedupe key for one chain-continuation link.

    The parent pass's job id makes each link's key unique while remaining a
    verifiable extension of the chain's canonical identity (see
    :func:`parse_promotion_reconcile_payload`). A crash-retry of the parent
    pass recomputes the same key, so replay stays idempotent.
    """
    return f"{promotion_reconcile_dedupe_key(reason, trigger_id)}:{parent_job_id}"


def _dedupe_key_matches_chain(dedupe_key: str, reason: str, trigger_id: str) -> bool:
    """Whether ``dedupe_key`` is the chain key or a valid continuation link of it."""
    canonical = promotion_reconcile_dedupe_key(reason, trigger_id)
    if dedupe_key == canonical:
        return True
    prefix = canonical + ":"
    if not dedupe_key.startswith(prefix):
        return False
    try:
        uuid.UUID(dedupe_key[len(prefix) :])
    except ValueError:
        return False
    return True


def _require_nonempty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PromotionReconcileContractError(f"promotion.reconcile payload missing {field}")
    return value


def build_promotion_reconcile_payload(
    *,
    reason: str,
    trigger_id: str,
) -> dict[str, object]:
    """Construct one canonical, exact-field promotion-reconcile payload.

    The producer-side half of the contract: runs the same runtime validation
    :func:`parse_promotion_reconcile_payload` re-verifies on the worker side,
    and always computes the dedupe key itself from the validated identity
    fields. Callers can never supply their own ``dedupe_key``.
    """
    if reason not in PROMOTION_RECONCILE_REASONS:
        raise PromotionReconcileContractError(f"unknown promotion.reconcile reason: {reason!r}")
    validated_trigger_id = _require_nonempty_str(trigger_id, field="trigger_id")
    return {
        "contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION,
        "reason": reason,
        "trigger_id": validated_trigger_id,
        "dedupe_key": promotion_reconcile_dedupe_key(reason, validated_trigger_id),
    }


def parse_promotion_reconcile_payload(payload: dict[str, object]) -> PromotionReconcilePayload:
    """Parse and validate a ``promotion.reconcile`` payload (fail closed).

    Any field outside the closed set, an unknown contract version or reason,
    or a ``dedupe_key`` that does not equal the canonical key recomputed from
    the parsed identity raises :class:`PromotionReconcileContractError`.
    """
    if not isinstance(payload, dict):
        raise PromotionReconcileContractError("promotion.reconcile payload must be an object")
    contract_version = payload.get("contract_version")
    if contract_version != PROMOTION_RECONCILE_CONTRACT_VERSION:
        raise PromotionReconcileContractError(
            f"unsupported promotion.reconcile contract_version: {contract_version!r}"
        )
    unknown_fields = set(payload) - PROMOTION_RECONCILE_ALLOWED_FIELDS
    if unknown_fields:
        raise PromotionReconcileContractError(
            "promotion.reconcile payload carries unsupported field(s): "
            f"{sorted(unknown_fields)!r}"
        )
    reason = payload.get("reason")
    if reason not in PROMOTION_RECONCILE_REASONS:
        raise PromotionReconcileContractError(f"unknown promotion.reconcile reason: {reason!r}")
    assert isinstance(reason, str)
    trigger_id = _require_nonempty_str(payload.get("trigger_id"), field="trigger_id")
    dedupe_key = _require_nonempty_str(payload.get("dedupe_key"), field="dedupe_key")
    if not _dedupe_key_matches_chain(dedupe_key, reason, trigger_id):
        expected = promotion_reconcile_dedupe_key(reason, trigger_id)
        raise PromotionReconcileContractError(
            f"promotion.reconcile dedupe_key {dedupe_key!r} does not match the canonical "
            f"identity key {expected!r} or a valid continuation link of it"
        )
    return PromotionReconcilePayload(
        contract_version=contract_version,
        reason=reason,
        trigger_id=trigger_id,
        dedupe_key=dedupe_key,
    )


# ---------------------------------------------------------------------------
# Pass result (content-free counts)
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationPassResult:
    """Content-free outcome of one bounded reconciliation pass."""

    tenant_id: str
    reason: str
    trigger_id: str
    window_size: int = 0
    wrapped: bool = False
    evaluations_enqueued: int = 0
    dead_found: int = 0
    missing_found: int = 0
    recovery_enqueued: int = 0
    terminal_skipped: int = 0
    healthy_skipped: int = 0
    suppressed: int = 0
    chain_continued: bool = False

    def summarize(self) -> str:
        return (
            f"tenant={self.tenant_id} reason={self.reason} trigger={self.trigger_id} "
            f"window={self.window_size} wrapped={self.wrapped} "
            f"evaluations={self.evaluations_enqueued} dead={self.dead_found} "
            f"missing={self.missing_found} recovery={self.recovery_enqueued} "
            f"terminal={self.terminal_skipped} healthy={self.healthy_skipped} "
            f"suppressed={self.suppressed} continued={self.chain_continued}"
        )


@dataclass(frozen=True)
class TenantSchedulingResult:
    """Content-free result of one bounded owner-side tenant window."""

    inspected: int
    enqueued: int
    wrapped: bool = False
    completed: bool = False


# Repair classification of one candidate, derived exclusively from the shared
# evaluator's PromotionCandidate (never a second policy).
_REPAIR_ELIGIBLE_NOW = "eligible_now"
_REPAIR_COOLING = "cooling"
_REPAIR_TERMINAL = "terminal"


def _lane_trust(candidate: Any) -> tuple[bool, bool]:
    legacy_trust = candidate.legacy_confidence >= candidate.legacy_threshold
    evidence_trust = (
        candidate.evidence_score is not None
        and candidate.evidence_score >= candidate.evidence_threshold
    )
    return legacy_trust, evidence_trust


def _classify_repair(candidate: Any) -> str:
    if candidate.would_promote:
        # All static gates pass; only the promotion-time conflict recheck
        # (which the evaluator itself runs) remains.
        return _REPAIR_ELIGIBLE_NOW
    if candidate.selected_basis is not None:
        # A lane was selected but conflict/dispute/review-policy blocks the
        # mutation: only external action (conflict resolution, review, human
        # verification) can unblock — never re-evaluation alone.
        return _REPAIR_TERMINAL
    legacy_trust, evidence_trust = _lane_trust(candidate)
    if legacy_trust or evidence_trust:
        return _REPAIR_COOLING
    return _REPAIR_TERMINAL


def _next_boundary(candidate: Any, moment: datetime) -> tuple[datetime, bool, bool]:
    """The earliest authoritative eligibility boundary among trust-qualified
    lanes, plus the lane trust flags. Uses the shared evaluator's own
    eligible_at math (created_at / cooling start + auto_promote_min_age_hours)
    — never a duplicate age/evidence formula."""
    legacy_trust, evidence_trust = _lane_trust(candidate)
    boundaries: list[datetime] = []
    if candidate.eligible_at is not None:
        boundaries.append(candidate.eligible_at)
    if legacy_trust:
        boundaries.append(candidate.legacy_eligible_at)
    if evidence_trust and candidate.evidence_eligible_at is not None:
        boundaries.append(candidate.evidence_eligible_at)
    if not boundaries:
        # Unreachable for eligible/cooling classifications, but fail safe:
        # treat as due now rather than inventing a boundary.
        return moment, legacy_trust, evidence_trust
    return min(boundaries), legacy_trust, evidence_trust


def _requested_policy_version(candidate: Any, legacy_trust: bool, evidence_trust: bool) -> str:
    basis = candidate.selected_basis
    if basis is None:
        basis = "retention_evidence" if evidence_trust and not legacy_trust else "legacy_confidence"
    if basis == "retention_evidence":
        return EVIDENCE_PROMOTION_POLICY_VERSION
    return LEGACY_PROMOTION_POLICY_VERSION


def _repair_trigger(
    reason: str, state: PromotionReconcileState | None, boundary: datetime
) -> tuple[str, str]:
    """(trigger_type, trigger_id) for a repair evaluation.

    Stable per reconciliation observation: the identity pairs the reason's
    provenance (the kind-policy revision for policy chains, ``reconcile``
    otherwise) with the authoritative eligibility boundary the repair targets,
    so replaying the same observation produces the same dedupe key while new
    evidence (a moved boundary) legitimately represents a new observation.
    """
    if reason == RECONCILE_REASON_POLICY_CHANGE:
        revision = state.kind_policy_revision if state is not None else 0
        return TRIGGER_POLICY_CHANGED, f"kind-policy:{revision}:boundary:{boundary.isoformat()}"
    return TRIGGER_RECONCILE, f"reconcile:boundary:{boundary.isoformat()}"


# ---------------------------------------------------------------------------
# Window job-state lookup
# ---------------------------------------------------------------------------


@dataclass
class _ItemJobState:
    canonical_covers_obligation: bool = False
    legacy_covers_obligation: bool = False
    healthy_refine: bool = False
    dead_promotion: bool = False
    classification_intended: bool = False

    @property
    def promotion_covers_obligation(self) -> bool:
        return self.canonical_covers_obligation or self.legacy_covers_obligation


@dataclass(frozen=True)
class _ItemObligation:
    boundary: datetime | None
    classification_run_id: uuid.UUID | None


async def _window_job_states(
    session: AsyncSession,
    *,
    tenant_id: str,
    obligations: dict[uuid.UUID, _ItemObligation],
    now: datetime,
) -> dict[uuid.UUID, _ItemJobState]:
    """Resolve current queue coverage with indexed probes per window item.

    For a cooling obligation only the exact evaluator-produced boundary
    covers it.  For an already-due obligation, a due/overdue pending or
    running canonical job covers it; a future job does not.  Legacy Path A
    additionally has to name the currently bound classification run.  The
    VALUES relation has at most ``promotion_reconciliation_pass_limit`` rows,
    and every EXISTS probe is served by ``idx_jobs_reconcile_item_state``;
    unrelated history is neither materialized nor globally capped.
    """
    result = {item_id: _ItemJobState() for item_id in obligations}
    if not obligations:
        return result
    value_rows: list[str] = []
    params: dict[str, object] = {"tenant_id": str(tenant_id), "moment": now}
    for index, (item_id, obligation) in enumerate(obligations.items()):
        value_rows.append(
            f"(CAST(:item_{index} AS uuid), CAST(:boundary_{index} AS timestamptz), "
            f"CAST(:run_{index} AS uuid))"
        )
        params[f"item_{index}"] = str(item_id)
        params[f"boundary_{index}"] = obligation.boundary
        params[f"run_{index}"] = (
            str(obligation.classification_run_id)
            if obligation.classification_run_id is not None
            else None
        )
    rows = (
        await session.execute(
            text(
                "WITH obligation(item_id, boundary, classification_run_id) AS (VALUES "
                + ", ".join(value_rows)
                + ") SELECT o.item_id, "
                "EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id = CAST(:tenant_id AS uuid) "
                "AND j.payload->>'memory_item_id' = o.item_id::text "
                "AND j.job_type = 'promotion.evaluate' "
                "AND j.status IN ('pending', 'running') AND o.boundary IS NOT NULL "
                "AND ((o.boundary > CAST(:moment AS timestamptz) AND j.run_after = o.boundary) "
                "OR (o.boundary <= CAST(:moment AS timestamptz) "
                "AND j.run_after <= CAST(:moment AS timestamptz)))) AS canonical_covers, "
                "EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id = CAST(:tenant_id AS uuid) "
                "AND j.payload->>'memory_item_id' = o.item_id::text "
                "AND j.job_type = 'promotion.path_a' "
                "AND j.status IN ('pending', 'running') AND o.boundary IS NOT NULL "
                "AND o.classification_run_id IS NOT NULL "
                "AND j.payload->>'classification_run_id' = o.classification_run_id::text "
                "AND ((o.boundary > CAST(:moment AS timestamptz) AND j.run_after = o.boundary) "
                "OR (o.boundary <= CAST(:moment AS timestamptz) "
                "AND j.run_after <= CAST(:moment AS timestamptz)))) AS legacy_covers, "
                "EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id = CAST(:tenant_id AS uuid) "
                "AND j.payload->>'memory_item_id' = o.item_id::text "
                "AND j.job_type = 'classification.refine' "
                "AND j.status IN ('pending', 'running')) AS healthy_refine, "
                "EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id = CAST(:tenant_id AS uuid) "
                "AND j.payload->>'memory_item_id' = o.item_id::text "
                "AND j.job_type IN ('promotion.evaluate', 'promotion.path_a') "
                "AND j.status = 'dead') AS dead_promotion, "
                "EXISTS (SELECT 1 FROM item_events e WHERE e.item_id = o.item_id "
                "AND e.event_type = 'classification' AND e.field_name = 'kind' "
                "AND e.new_value::jsonb->>'source' = 'auto_classified') "
                "AS classification_intended FROM obligation o"
            ),
            params,
        )
    ).mappings()
    for row in rows:
        item_id = row["item_id"]
        result[item_id] = _ItemJobState(
            canonical_covers_obligation=bool(row["canonical_covers"]),
            legacy_covers_obligation=bool(row["legacy_covers"]),
            healthy_refine=bool(row["healthy_refine"]),
            dead_promotion=bool(row["dead_promotion"]),
            classification_intended=bool(row["classification_intended"]),
        )
    return result


# ---------------------------------------------------------------------------
# The bounded pass
# ---------------------------------------------------------------------------


async def _active_chain_job(
    session: AsyncSession,
    *,
    tenant_id: str,
    reason: str,
    trigger_id: str,
) -> uuid.UUID | None:
    """The id of this chain's pending/running link, if one exists.

    Matches on the payload's exact ``(reason, trigger_id)`` identity — closed
    envelope fields, never free-text metadata — so it recognizes any link of
    the chain (the first link carries the canonical chain key; continuations
    carry parent-suffixed link keys). Bounded by the in-flight
    pending/running subset via idx_jobs_tenant_type_status.
    """
    return (
        await session.execute(
            select(Job.id)
            .where(
                Job.tenant_id == str(tenant_id),
                Job.job_type == PROMOTION_RECONCILE_JOB_TYPE,
                Job.status.in_(("pending", "running")),
                text("payload->>'reason' = :reason"),
                text("payload->>'trigger_id' = :trigger_id"),
            )
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(1)
            .params(reason=reason, trigger_id=trigger_id)
        )
    ).scalar_one_or_none()


def _window_stmt(
    tenant_id: str,
    cursor: PromotionReconcileState | None,
    limit: int,
) -> Select[tuple[MemoryItem]]:
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.review_status == "proposed",
        MemoryItem.valid_to.is_(None),
        MemoryItem.superseded_by.is_(None),
        _kind_promotion_allowed(),
        ~select(PromotionReconcileTerminal.item_id)
        .where(
            PromotionReconcileTerminal.tenant_id == MemoryItem.tenant_id,
            PromotionReconcileTerminal.item_id == MemoryItem.id,
            PromotionReconcileTerminal.cursor_epoch
            == (cursor.cursor_epoch if cursor is not None else 0),
        )
        .exists(),
    )
    if cursor is not None and cursor.cursor_created_at is not None:
        stmt = stmt.where(
            or_(
                MemoryItem.created_at > cursor.cursor_created_at,
                and_(
                    MemoryItem.created_at == cursor.cursor_created_at,
                    MemoryItem.id > cursor.cursor_item_id,
                ),
            )
        )
    return (
        stmt.order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
        .limit(limit)
        .with_for_update(of=MemoryItem)
    )


async def run_reconciliation_pass(
    session: AsyncSession,
    tenant_id: str,
    *,
    reason: str,
    trigger_id: str,
    now: datetime | None = None,
    self_job_id: uuid.UUID | str | None = None,
) -> ReconciliationPassResult:
    """Run one bounded reconciliation pass and commit it atomically.

    Everything the pass represents — repair enqueues, cursor advance,
    diagnostics, and the chain continuation — commits in this function's single
    transaction, so a crash before commit replays safely from the unchanged
    cursor and a crash after commit leaves durable, exactly-once-enqueued
    work. The caller (the worker's ``promotion.reconcile`` handler) supplies
    an app-role session already RLS-scoped to ``tenant_id``: item discovery
    and evaluation routing run under the normal tenant context, never caller
    authority. ``self_job_id`` is the queued job executing this pass; it
    namespaces the continuation link's dedupe key (see
    :func:`promotion_reconcile_continuation_key`).
    """
    from engram.jobs import enqueue_job_in_transaction

    if reason not in PROMOTION_RECONCILE_REASONS:
        raise PromotionReconcileContractError(f"unknown promotion.reconcile reason: {reason!r}")
    moment = now or datetime.now(UTC)
    result = ReconciliationPassResult(
        tenant_id=str(tenant_id), reason=reason, trigger_id=trigger_id
    )
    # populate_existing: this session may hold an identity-mapped state row
    # from a previous pass in the same session whose in-memory cursor predates
    # that pass's own Core upsert (which bypasses the ORM). Without the forced
    # refresh, a follow-up pass could read a stale cursor position and re-work
    # an already-covered window instead of advancing/wrapping.
    state: PromotionReconcileState | None = (
        await session.execute(
            select(PromotionReconcileState)
            .where(PromotionReconcileState.tenant_id == uuid.UUID(str(tenant_id)))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    limit = settings.promotion_reconciliation_pass_limit
    items = list((await session.execute(_window_stmt(tenant_id, state, limit))).scalars())
    rotation_completed = False
    if not items and state is not None and state.cursor_created_at is not None:
        # The keyset page after the cursor is empty: this chain reached the
        # tail of the live proposed set. The perpetual backstop wraps to the
        # head and keeps rotating; a request chain has completed its
        # post-reset rotation and terminates here (full coverage since head).
        result.wrapped = True
        rotation_completed = True
        if reason == RECONCILE_REASON_BACKSTOP:
            items = list(
                (await session.execute(_window_stmt(tenant_id, None, limit))).scalars()
            )
    result.window_size = len(items)

    config = await _config(session, str(tenant_id))
    enabled, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    support_map = await load_promotion_support(session, items)
    candidates: dict[uuid.UUID, Any] = {}
    repair_classes: dict[uuid.UUID, str] = {}
    obligations: dict[uuid.UUID, _ItemObligation] = {}
    for item in items:
        candidate = assess_promotion_candidate(
            item,
            support_map[item.id],
            confidence_threshold=threshold,
            min_age_hours=min_age,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=moment,
        )
        repair_class = _classify_repair(candidate)
        candidates[item.id] = candidate
        repair_classes[item.id] = repair_class
        boundary = (
            _next_boundary(candidate, moment)[0]
            if repair_class != _REPAIR_TERMINAL
            else None
        )
        obligations[item.id] = _ItemObligation(
            boundary=boundary,
            classification_run_id=candidate.classification_run_id,
        )
    job_states = await _window_job_states(
        session,
        tenant_id=tenant_id,
        obligations=obligations,
        now=moment,
    )
    classification_available = settings.classification_provider != "none"

    for item in items:
        if item.superseded_by is not None:
            # Same liveness skip the mutation sweep applies.
            result.terminal_skipped += 1
            continue
        candidate = candidates[item.id]
        repair_class = repair_classes[item.id]

        # Provider recovery restores only remember-time classification intent
        # proven by the immutable initial auto_classified audit event.  Lack
        # of a receipt alone is not proof: explicit-kind and unknown legacy
        # rows stay excluded.  Work remains async and already-bound evidence
        # is never reclassified.
        if (
            reason == RECONCILE_REASON_PROVIDER_RECOVERY
            and support_map[item.id].classification_run is None
            and not job_states[item.id].healthy_refine
            and job_states[item.id].classification_intended
        ):
            if classification_available and enabled:
                await enqueue_job_in_transaction(
                    session,
                    tenant_id=tenant_id,
                    job_type=_CLASSIFICATION_REFINE_JOB_TYPE,
                    payload={"memory_item_id": str(item.id)},
                    dedupe_key=f"{_CLASSIFICATION_REFINE_JOB_TYPE}:{item.id}",
                )
                result.recovery_enqueued += 1
            else:
                result.suppressed += 1

        if repair_class == _REPAIR_TERMINAL:
            # Persist only scheduler-level suppression for this epoch.  Any
            # relevant item/evidence/feedback event deletes it; a full reset
            # advances the epoch.  No promotion decision data is retained.
            await session.execute(
                pg_insert(PromotionReconcileTerminal)
                .values(
                    tenant_id=uuid.UUID(str(tenant_id)),
                    item_id=item.id,
                    cursor_epoch=state.cursor_epoch if state is not None else 0,
                    observed_at=moment,
                )
                .on_conflict_do_update(
                    index_elements=[
                        PromotionReconcileTerminal.tenant_id,
                        PromotionReconcileTerminal.item_id,
                    ],
                    set_={
                        "cursor_epoch": state.cursor_epoch if state is not None else 0,
                        "observed_at": moment,
                    },
                )
            )
            result.terminal_skipped += 1
            continue
        if job_states[item.id].promotion_covers_obligation:
            # Status alone is insufficient: this job's due time (and, for
            # legacy work, classification binding) covers the evaluator's
            # current authoritative obligation.
            result.healthy_skipped += 1
            continue
        if job_states[item.id].dead_promotion:
            result.dead_found += 1
        else:
            result.missing_found += 1
        if not enabled or not settings.promotion_evaluate_jobs_enabled:
            # Tenant promotion disabled, or the canonical evaluate rollout
            # flag off: reconciliation must not silently substitute a
            # broader/legacy mutation mechanism — record and move on.
            result.suppressed += 1
            continue
        boundary, legacy_trust, evidence_trust = _next_boundary(candidate, moment)
        trigger_type, repair_trigger_id = _repair_trigger(reason, state, boundary)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=item.id,
            trigger_type=trigger_type,
            trigger_id=repair_trigger_id,
            requested_policy_version=_requested_policy_version(
                candidate, legacy_trust, evidence_trust
            ),
            # The exact authoritative eligibility boundary — never earlier.
            # A boundary already in the past means the repair is due now.
            run_after=boundary if boundary > moment else moment,
        )
        result.evaluations_enqueued += 1

    # --- Durable state + chain continuation, same transaction ---------------
    values: dict[str, Any] = {
        "last_pass_at": moment,
        "last_pass_reason": reason,
        "last_pass_trigger_id": trigger_id,
        "last_window_size": result.window_size,
        "last_wrapped": result.wrapped,
        "last_evaluations_enqueued": result.evaluations_enqueued,
        "last_dead_found": result.dead_found,
        "last_missing_found": result.missing_found,
        "last_recovery_enqueued": result.recovery_enqueued,
        "last_terminal_skipped": result.terminal_skipped,
        "last_healthy_skipped": result.healthy_skipped,
        "last_suppressed": result.suppressed,
        "updated_at": moment,
    }
    read_epoch = state.cursor_epoch if state is not None else 0
    if items:
        values.update(
            {
                "cursor_created_at": items[-1].created_at,
                "cursor_item_id": items[-1].id,
            }
        )
    await session.execute(
        pg_insert(PromotionReconcileState)
        .values(tenant_id=uuid.UUID(str(tenant_id)), cursor_epoch=read_epoch, **values)
        .on_conflict_do_update(
            index_elements=[PromotionReconcileState.tenant_id],
            set_=values,
            # Epoch guard: a pass may only advance the cursor while the epoch
            # it read is still current. A concurrent reset (policy change /
            # operator request) bumped the epoch, so this stale pass no-ops
            # instead of overwriting the post-reset head position.
            where=PromotionReconcileState.cursor_epoch == read_epoch,
        )
    )

    if reason == RECONCILE_REASON_BACKSTOP:
        continuation_key = (
            promotion_reconcile_continuation_key(
                RECONCILE_REASON_BACKSTOP, BACKSTOP_TRIGGER_ID, self_job_id
            )
            if self_job_id is not None
            else promotion_reconcile_dedupe_key(RECONCILE_REASON_BACKSTOP, BACKSTOP_TRIGGER_ID)
        )
        await enqueue_job_in_transaction(
            session,
            tenant_id=tenant_id,
            job_type=PROMOTION_RECONCILE_JOB_TYPE,
            payload=build_promotion_reconcile_payload(
                reason=RECONCILE_REASON_BACKSTOP, trigger_id=BACKSTOP_TRIGGER_ID
            ),
            run_after=moment
            + timedelta(seconds=settings.promotion_reconciliation_interval_seconds),
            dedupe_key=continuation_key,
        )
        result.chain_continued = True
    elif not rotation_completed and result.window_size > 0:
        # Request chain: immediately-due durable continuation until the
        # post-reset rotation reaches the tail, then stop. The link-suffixed
        # dedupe key lets the next pass coexist with this still-running one
        # (the canonical key alone would dedupe into the running job and
        # silently kill the chain), while a crash-retry of this pass
        # recomputes the same key, so replay stays idempotent and a huge
        # tenant fans out as a bounded sequence rather than a burst of jobs.
        continuation_key = (
            promotion_reconcile_continuation_key(reason, trigger_id, self_job_id)
            if self_job_id is not None
            else promotion_reconcile_dedupe_key(reason, trigger_id)
        )
        await enqueue_job_in_transaction(
            session,
            tenant_id=tenant_id,
            job_type=PROMOTION_RECONCILE_JOB_TYPE,
            payload=build_promotion_reconcile_payload(reason=reason, trigger_id=trigger_id),
            run_after=moment,
            dedupe_key=continuation_key,
        )
        result.chain_continued = True

    await session.commit()
    logger.info("promotion.reconcile %s", result.summarize())
    return result


# ---------------------------------------------------------------------------
# Request entry points (committed policy changes / explicit operator requests)
# ---------------------------------------------------------------------------


async def bump_kind_policy_revision(session: AsyncSession, tenant_id: str) -> int:
    """Increment and return the tenant's kind-policy revision.

    The stable, monotonically increasing identity for admission-affecting
    memory-kind changes — the value policy-change reconciliation trigger ids
    are built from, so replaying/deduplicating a policy reconciliation is a
    function of committed state rather than wall-clock timestamps. Runs in
    the caller's transaction (the kind-update route commits both together).
    """
    revision = (
        await session.execute(
            pg_insert(PromotionReconcileState)
            .values(
                tenant_id=uuid.UUID(str(tenant_id)),
                cursor_epoch=0,
                kind_policy_revision=1,
                last_wrapped=False,
            )
            .on_conflict_do_update(
                index_elements=[PromotionReconcileState.tenant_id],
                set_={
                    "kind_policy_revision": PromotionReconcileState.kind_policy_revision + 1,
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(PromotionReconcileState.kind_policy_revision)
        )
    ).scalar_one()
    return int(revision)


async def request_reconciliation_chain(
    session: AsyncSession,
    *,
    tenant_id: str,
    reason: str,
    trigger_id: str,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Reset the tenant's rotation cursor and enqueue a reconciliation chain.

    The bounded, non-synchronous answer to "promotion policy/config changed;
    reconcile this tenant now": exactly one ``promotion.reconcile`` job is
    enqueued (never thousands of item jobs), and the chain's durable
    continuation provides bounded full-rotation coverage. The cursor reset
    (cursor → NULL, epoch + 1) commits atomically with the enqueue, so a
    crash between the two cannot leave a request that silently skips the
    head of the backlog. Returns the job id, or ``None`` when the rollout
    flag is off (fail-safe no-op, no state change).
    """
    from engram.jobs import enqueue_job_in_transaction

    if reason not in _REQUEST_REASONS:
        raise PromotionReconcileContractError(
            "request_reconciliation_chain accepts request reasons only "
            f"(policy_change/provider_recovery/operator_request), got {reason!r}"
        )
    if not settings.promotion_reconciliation_enabled:
        return None
    moment = now or datetime.now(UTC)
    # Idempotent for the same explicit request identity: if any link of this
    # chain is still pending/running, return it instead of creating a
    # parallel chain (the first link's canonical key already enforces this
    # while it is the active link; this extends the guarantee across the
    # whole chain).
    active = await _active_chain_job(
        session, tenant_id=str(tenant_id), reason=reason, trigger_id=trigger_id
    )
    if active is not None:
        return active
    await session.execute(
        pg_insert(PromotionReconcileState)
        .values(
            tenant_id=uuid.UUID(str(tenant_id)),
            cursor_epoch=1,
            kind_policy_revision=0,
            last_wrapped=False,
        )
        .on_conflict_do_update(
            index_elements=[PromotionReconcileState.tenant_id],
            set_={
                "cursor_created_at": None,
                "cursor_item_id": None,
                "cursor_epoch": PromotionReconcileState.cursor_epoch + 1,
                "updated_at": moment,
            },
        )
    )
    return await enqueue_job_in_transaction(
        session,
        tenant_id=tenant_id,
        job_type=PROMOTION_RECONCILE_JOB_TYPE,
        payload=build_promotion_reconcile_payload(reason=reason, trigger_id=trigger_id),
        run_after=moment,
        dedupe_key=promotion_reconcile_dedupe_key(reason, trigger_id),
    )


async def _locked_tenant_scheduler_state(
    owner_session: AsyncSession, scheduler_key: str
) -> PromotionReconcileSchedulerState:
    await owner_session.execute(
        pg_insert(PromotionReconcileSchedulerState)
        .values(scheduler_key=scheduler_key, completed=False)
        .on_conflict_do_nothing(index_elements=[PromotionReconcileSchedulerState.scheduler_key])
    )
    return (
        await owner_session.execute(
            select(PromotionReconcileSchedulerState)
            .where(PromotionReconcileSchedulerState.scheduler_key == scheduler_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _tenant_window_stmt(
    state: PromotionReconcileSchedulerState,
    limit: int,
) -> Select[tuple[uuid.UUID]]:
    stmt = select(Tenant.id)
    if state.cursor_created_at is not None:
        stmt = stmt.where(
            or_(
                Tenant.created_at > state.cursor_created_at,
                and_(
                    Tenant.created_at == state.cursor_created_at,
                    Tenant.id > state.cursor_tenant_id,
                ),
            )
        )
    return stmt.order_by(Tenant.created_at.asc(), Tenant.id.asc()).limit(limit)


async def _schedule_tenant_window(
    owner_session: AsyncSession,
    *,
    scheduler_key: str,
    reason: str,
    trigger_id: str,
    periodic: bool,
) -> TenantSchedulingResult:
    """Inspect one locked, restart-safe tenant keyset window."""
    from engram.jobs import enqueue_job_in_transaction

    state = await _locked_tenant_scheduler_state(owner_session, scheduler_key)
    if state.completed and not periodic:
        return TenantSchedulingResult(inspected=0, enqueued=0, completed=True)
    limit = settings.promotion_reconciliation_tenant_batch_limit
    tenant_rows = (await owner_session.execute(_tenant_window_stmt(state, limit))).all()
    wrapped = False
    if not tenant_rows and state.cursor_created_at is not None:
        if periodic:
            wrapped = True
            state.cursor_created_at = None
            state.cursor_tenant_id = None
            tenant_rows = (
                await owner_session.execute(_tenant_window_stmt(state, limit))
            ).all()
        else:
            state.completed = True
            state.updated_at = datetime.now(UTC)
            return TenantSchedulingResult(inspected=0, enqueued=0, completed=True)
    enqueued = 0
    moment = datetime.now(UTC)
    for (tenant_id,) in tenant_rows:
        active = await _active_chain_job(
            owner_session,
            tenant_id=str(tenant_id),
            reason=reason,
            trigger_id=trigger_id,
        )
        if active is not None:
            continue
        if periodic:
            await enqueue_job_in_transaction(
                owner_session,
                tenant_id=str(tenant_id),
                job_type=PROMOTION_RECONCILE_JOB_TYPE,
                payload=build_promotion_reconcile_payload(
                    reason=reason, trigger_id=trigger_id
                ),
                run_after=moment,
                dedupe_key=promotion_reconcile_dedupe_key(reason, trigger_id),
            )
        else:
            await request_reconciliation_chain(
                owner_session,
                tenant_id=str(tenant_id),
                reason=reason,
                trigger_id=trigger_id,
                now=moment,
            )
        enqueued += 1
    if tenant_rows:
        last_tenant_id = tenant_rows[-1][0]
        last_created_at = (
            await owner_session.execute(
                select(Tenant.created_at).where(Tenant.id == last_tenant_id)
            )
        ).scalar_one()
        state.cursor_created_at = last_created_at
        state.cursor_tenant_id = last_tenant_id
    if not periodic and len(tenant_rows) < limit:
        state.completed = True
    state.updated_at = moment
    return TenantSchedulingResult(
        inspected=len(tenant_rows),
        enqueued=enqueued,
        wrapped=wrapped,
        completed=state.completed,
    )


async def ensure_periodic_reconciliation_chains(owner_session: AsyncSession) -> int:
    """Bootstrap/heal one bounded tenant window of backstop chains.

    The owner-only cursor is committed atomically with queue inserts.  Each
    call inspects at most ``promotion_reconciliation_tenant_batch_limit``
    tenant ids, wraps fairly, and resumes after process restart.
    """
    if not settings.promotion_reconciliation_enabled:
        return 0
    result = await _schedule_tenant_window(
        owner_session,
        scheduler_key="periodic-backstop-v1",
        reason=RECONCILE_REASON_BACKSTOP,
        trigger_id=BACKSTOP_TRIGGER_ID,
        periodic=True,
    )
    await owner_session.commit()
    return result.enqueued


async def request_global_reconciliation_window(
    owner_session: AsyncSession,
    *,
    reason: str,
    trigger_id: str,
) -> TenantSchedulingResult:
    """Request one restart-safe bounded all-tenant CLI continuation page."""
    if reason not in _REQUEST_REASONS:
        raise PromotionReconcileContractError(f"invalid global request reason: {reason!r}")
    if not settings.promotion_reconciliation_enabled:
        return TenantSchedulingResult(inspected=0, enqueued=0, completed=True)
    result = await _schedule_tenant_window(
        owner_session,
        scheduler_key=f"explicit-request-v1:{reason}:{trigger_id}",
        reason=reason,
        trigger_id=trigger_id,
        periodic=False,
    )
    await owner_session.commit()
    return result


# ---------------------------------------------------------------------------
# Diagnostics (content-free)
# ---------------------------------------------------------------------------


async def reconciliation_status(
    session: AsyncSession,
    *,
    tenant_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Content-free operational snapshot of the backstop for one tenant.

    Reuses the canonical job-state vocabulary (pending/running/dead) and the
    persisted cursor/diagnostics — doctor presents this, it never computes
    promotion policy of its own.
    """
    state: PromotionReconcileState | None = (
        await session.execute(
            select(PromotionReconcileState).where(
                PromotionReconcileState.tenant_id == uuid.UUID(str(tenant_id))
            )
        )
    ).scalar_one_or_none()
    rows = (
        (
            await session.execute(
                select(Job.job_type, Job.status, Job.payload)
                .where(
                    Job.tenant_id == str(tenant_id),
                    Job.job_type == PROMOTION_RECONCILE_JOB_TYPE,
                    Job.status.in_(("pending", "running", "dead")),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
                .limit(_MAX_DIAGNOSTIC_JOB_ROWS)
            )
        )
        .all()
    )
    pending_by_reason: dict[str, int] = {}
    dead_total = 0
    for _job_type, job_status, payload in rows:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        reason = reason if isinstance(reason, str) else "unknown"
        if job_status in ("pending", "running"):
            pending_by_reason[reason] = pending_by_reason.get(reason, 0) + 1
        elif job_status == "dead":
            dead_total += 1
    return {
        "enabled": settings.promotion_reconciliation_enabled,
        "evaluate_jobs_enabled": settings.promotion_evaluate_jobs_enabled,
        "interval_seconds": settings.promotion_reconciliation_interval_seconds,
        "pass_limit": settings.promotion_reconciliation_pass_limit,
        "cursor": {
            "epoch": state.cursor_epoch if state is not None else 0,
            "position": (
                {
                    "created_at": state.cursor_created_at.isoformat(),
                    "item_id": str(state.cursor_item_id),
                }
                if state is not None and state.cursor_created_at is not None
                else None
            ),
            "kind_policy_revision": (
                state.kind_policy_revision if state is not None else 0
            ),
        },
        "last_pass": (
            {
                "at": state.last_pass_at.isoformat() if state.last_pass_at else None,
                "reason": state.last_pass_reason,
                "trigger_id": state.last_pass_trigger_id,
                "window_size": state.last_window_size,
                "wrapped": state.last_wrapped,
                "evaluations_enqueued": state.last_evaluations_enqueued,
                "dead_found": state.last_dead_found,
                "missing_found": state.last_missing_found,
                "recovery_enqueued": state.last_recovery_enqueued,
                "terminal_skipped": state.last_terminal_skipped,
                "healthy_skipped": state.last_healthy_skipped,
                "suppressed": state.last_suppressed,
            }
            if state is not None and state.last_pass_at is not None
            else None
        ),
        "chains": {
            "pending_by_reason": dict(sorted(pending_by_reason.items())),
            "dead_jobs": dead_total,
        },
    }
